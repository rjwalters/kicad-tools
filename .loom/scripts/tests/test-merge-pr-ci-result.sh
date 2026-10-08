#!/usr/bin/env bash
# test-merge-pr-ci-result.sh - Tests for merge-pr.sh's CI-run conclusion gate
# (_check_ci_result, #10444; fail-closed since #10567).
#
# The decision is `loom-daemon merge-pr ci-result` (Rust,
# loom-daemon/src/merge_pr/ci_result.rs, with its own unit tests). This suite
# pins the merge-pr.sh WIRING against a fake native binary (LOOM_DAEMON_BIN)
# and a fake forge merge call (forge_merge_pr, recorded to a file): every case
# runs the guard and then "the merge", exactly as the script does, and asserts
# the merge is reached ONLY on a positive verdict.
#
#   CLEAN (exact-head run concluded success)          -> merges
#   NO-CI-WORKFLOW (repo defines no `CI` workflow)    -> merges (explicit policy)
#   refusal, exit 1 (failed/cancelled CI run while
#     the required contexts are green, #10403)        -> exit 1, never merges
#   UNVERIFIED exit 3 (absent / in-progress run),
#     a legacy exit-0 UNVERIFIED, a moved head        -> HOLD exit 5, never merges
#   forge read failure (exit 2 + reason)              -> bounded retries, then exit 1
#   old binary (exit 2, unrecognized subcommand),
#     missing binary, empty/unexpected output         -> exit 1 + roll hint
#
# Before #10567 every unknown above warned and MERGED — an unavailable verifier
# was treated as permission to continue.
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
assert_not_contains() { if grep -qF -- "$2" <<<"$1"; then fail "$3" "unexpected '$2' in: $1"; else ok "$3"; fi; }

info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*"; }
error()   { echo "ERROR: $*" >&2; exit 1; }

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-ci-result.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

FUNCS="$WORK/funcs.sh"
# Both are one-liners (merge-pr.sh is held by the file-size and shell-budget ratchets).
awk '/^_mp_daemon_roll_hint\(\) \{/ || /^_check_ci_result\(\) \{/ { print }' "$MERGE_PR_SRC" > "$FUNCS"
grep -q '^_check_ci_result() {.*; }$' "$FUNCS" || { echo "FATAL: could not extract _check_ci_result" >&2; exit 2; }
grep -q '^_mp_daemon_roll_hint()' "$FUNCS" || { echo "FATAL: could not extract _mp_daemon_roll_hint" >&2; exit 2; }
# shellcheck disable=SC1090
source "$FUNCS"

# --- Structural wiring: where the gate sits relative to the merge call ---
echo "Testing _check_ci_result wiring..."
gate_line="$(awk '/^_check_ci_result$/ { print NR; exit }' "$MERGE_PR_SRC")"
merge_line="$(awk '/forge_merge_pr "\$REPO_NWO" "\$PR_NUMBER" "\$MERGE_PRECONDITION_SHA"/ { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$gate_line" && -n "$merge_line" && "$gate_line" -lt "$merge_line" ]]; then
    ok "gate runs before the forge merge call"
else
    fail "gate runs before the forge merge call" "gate=$gate_line merge=$merge_line"
fi
if awk '/^_revalidate_merge_guards\(\) \{/ { inf=1 } inf && /^  .*_check_ci_result settled$/ { found=1 } inf && /^\}/ { exit } END { exit !found }' "$MERGE_PR_SRC"; then
    ok "--auto's post-wait revalidation re-runs the gate"
else
    fail "--auto's post-wait revalidation re-runs the gate" "_check_ci_result settled not called inside _revalidate_merge_guards"
fi
regate_line="$(awk '/\[\[ "\$MERGE_PRECONDITION_SHA" == "\$PR_HEAD_SHA" .*\|\| _check_ci_result settled$/ { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$regate_line" && -n "$merge_line" && "$regate_line" -lt "$merge_line" ]]; then
    ok "a head moved by the precondition read is re-gated before the merge call"
else
    fail "a head moved by the precondition read is re-gated before the merge call" "regate=$regate_line merge=$merge_line"
fi
body="$(grep '^_check_ci_result() {' "$MERGE_PR_SRC")"
assert_not_contains "$body" "ALLOW" "no permissive bypass flag in the gate"
assert_not_contains "$body" "SKIP" "no permissive skip override in the gate"

# --- Fake native binary ---
make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" >> '$WORK/argv-$mode'"
        case "$mode" in
            clean)       echo "echo LOOM-CI-RESULT-CLEAN" ;;
            noci)        echo "echo 'LOOM-CI-RESULT-NO-CI-WORKFLOW the repository defines no \`CI\` workflow, so there is no CI run to gate head deadbeef on'" ;;
            cancelled)   echo "echo 'Merge blocked: PR #7 CI run concluded failure. Failed/cancelled jobs: Detect Changes (cancelled), Repo Hygiene Checks (cancelled). Re-run in place with gh run rerun --failed 2'; exit 1" ;;
            failed)      echo "echo 'Merge blocked: PR #7 CI run concluded failure. Failed/cancelled jobs: Rust Unit Tests (1/4) (failure).'; exit 1" ;;
            absent)      echo "echo 'LOOM-CI-RESULT-UNVERIFIED no \`CI\` workflow run found for head deadbeef, but the repository defines a \`CI\` workflow'; exit 3" ;;
            running)     echo "echo 'LOOM-CI-RESULT-UNVERIFIED \`CI\` run 9 for head deadbeef is still in_progress'; exit 3" ;;
            legacy)      echo "echo 'LOOM-CI-RESULT-UNVERIFIED no CI workflow run found'" ;;
            broken)      echo "echo 'ci-result could not query the forge: rate limited'; exit 2" ;;
            flaky)       echo "n=\$(wc -l < '$WORK/argv-flaky'); if [ \"\$n\" -lt 2 ]; then echo 'ci-result could not query the forge: HTTP 502'; exit 2; fi; echo LOOM-CI-RESULT-CLEAN" ;;
            old)         echo "echo \"error: unrecognized subcommand 'ci-result'\" >&2; exit 2" ;;
            garbage)     echo "echo 'something else entirely'" ;;
            empty)       echo "exit 0" ;;
            # Exact-head: CI only ever ran (green) on the OLD head.
            shaaware)    echo "case \"\$*\" in *'--head-sha deadbeef'*) echo LOOM-CI-RESULT-CLEAN ;; *) echo 'LOOM-CI-RESULT-UNVERIFIED no \`CI\` workflow run found for head'; exit 3 ;; esac" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

# --- Fake forge merge call: the thing that must never run on unknown/denied ---
forge_merge_pr() { echo "$*" >> "$WORK/merge-called"; }

PR_NUMBER=7; PR_HEAD_SHA=deadbeef; REPO_NWO=o/r; FORGE_TYPE=github; DRY_RUN=false; AUTO_MERGE=false
MERGE_PRECONDITION_SHA=""; LOOM_CI_RESULT_RETRY_DELAY=0; SCRIPT_DIR="$WORK"
LAST_OUT=""; LAST_RC=0
# run_guard [phase] — the gate, then "the merge", in a subshell like the script.
run_guard() {
    rm -f "$WORK/merge-called"
    set +e; LAST_OUT="$( (_check_ci_result "$@"; forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "${MERGE_PRECONDITION_SHA:-$PR_HEAD_SHA}" squash) 2>&1)"; LAST_RC=$?; set -e
}
merged()     { [[ -e "$WORK/merge-called" ]]; }
assert_merged()     { if merged; then ok "$1 -> merge call made"; else fail "$1 -> merge call made"; fi; }
assert_not_merged() { if merged; then fail "$1 -> merge call NEVER made" "$(cat "$WORK/merge-called")"; else ok "$1 -> merge call NEVER made"; fi; }

echo "Testing _check_ci_result verdicts..."

LOOM_DAEMON_BIN="$(make_stub clean)" run_guard
assert_eq 0 "$LAST_RC" "fully successful exact-head run -> passes"
assert_merged "CLEAN"
assert_contains "$(cat "$WORK/argv-clean")" "merge-pr ci-result --pr 7 --repo o/r --head-sha deadbeef" "daemon gets PR/repo/head operands"

LOOM_DAEMON_BIN="$(make_stub noci)" run_guard
assert_eq 0 "$LAST_RC" "repo with no CI workflow -> passes (explicit no-CI policy)"
assert_merged "NO-CI-WORKFLOW"
assert_contains "$LAST_OUT" "explicit no-CI policy" "no-CI pass is announced, not silent"

# The #10403 fixture: Detect Changes cancelled, required contexts green.
LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 1 "$LAST_RC" "cancelled Detect Changes (required contexts green) -> refused"
assert_not_merged "cancelled CI run"
assert_contains "$LAST_OUT" "Detect Changes (cancelled)" "refusal names the cancelled job"
assert_contains "$LAST_OUT" "gh run rerun --failed" "refusal suggests the in-place rerun"

LOOM_DAEMON_BIN="$(make_stub failed)" run_guard
assert_eq 1 "$LAST_RC" "failed CI run (required contexts green) -> refused"
assert_not_merged "failed CI run"

for mode in absent running; do
    LOOM_DAEMON_BIN="$(make_stub "$mode")" run_guard
    assert_eq 5 "$LAST_RC" "$mode run (exit 3) -> HOLD, exit 5"
    assert_not_merged "$mode run"
    assert_contains "$LAST_OUT" "Merge held: PR #7 head deadbeef" "$mode hold names PR + head SHA"
    assert_contains "$LAST_OUT" "Unknown is not success" "$mode hold states the reason"
    assert_contains "$LAST_OUT" "Re-attempt on a later pass" "$mode hold names the remediation"
done

LOOM_DAEMON_BIN="$(make_stub legacy)" run_guard
assert_eq 5 "$LAST_RC" "pre-#10567 binary's exit-0 UNVERIFIED -> HOLD, not pass"
assert_not_merged "legacy UNVERIFIED"
assert_contains "$LAST_OUT" "loom-daemon-update.sh --fetch" "legacy UNVERIFIED hold says to roll the host"

rm -f "$WORK/argv-broken"
LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 1 "$LAST_RC" "provider query failure (exit 2) -> refused after retries"
assert_not_merged "provider failure"
assert_eq 3 "$(wc -l < "$WORK/argv-broken" | tr -d ' ')" "provider failure retried, bounded at 3 attempts"
assert_contains "$LAST_OUT" "rate limited" "refusal quotes the provider's reason"
assert_contains "$LAST_OUT" "Merge blocked: PR #7 head deadbeef" "refusal names PR + head SHA"

: > "$WORK/argv-flaky"
LOOM_DAEMON_BIN="$(make_stub flaky)" run_guard
assert_eq 0 "$LAST_RC" "one transient provider failure, then CLEAN -> passes"
assert_merged "transient provider failure"

LOOM_DAEMON_BIN="$(make_stub old)" run_guard
assert_eq 1 "$LAST_RC" "old binary (exit 2, unrecognized subcommand) -> refused"
assert_not_merged "old binary"
assert_contains "$LAST_OUT" "REMEDIATION: this script requires loom-daemon >= " "old binary refusal carries the roll hint"
assert_contains "$LAST_OUT" "after 1 attempt(s)" "old binary is not retried"

LOOM_DAEMON_BIN="$WORK/does-not-exist" run_guard
assert_eq 1 "$LAST_RC" "missing binary -> refused"
assert_not_merged "missing binary"

for mode in garbage empty; do
    LOOM_DAEMON_BIN="$(make_stub "$mode")" run_guard
    assert_eq 1 "$LAST_RC" "$mode output with exit 0 -> refused"
    assert_not_merged "$mode output"
done

echo "Testing exact-head and --auto sequencing..."

# Head moved after CI ran green on the old head: the re-gate on the SHA the
# merge call will name has no run for it -> HOLD.
MERGE_PRECONDITION_SHA=c0ffee11 LOOM_DAEMON_BIN="$(make_stub shaaware)" run_guard settled
assert_eq 5 "$LAST_RC" "moved head (green CI only on the old head) -> HOLD"
assert_not_merged "moved head"
assert_contains "$(cat "$WORK/argv-shaaware")" "--head-sha c0ffee11" "re-gate queries the SHA being merged"
LOOM_DAEMON_BIN="$(make_stub shaaware)" run_guard
assert_eq 0 "$LAST_RC" "unmoved head -> passes on its own green run"

# --auto: the pre-wait call defers an unconcluded run; the post-wait call decides.
AUTO_MERGE=true LOOM_DAEMON_BIN="$(make_stub running)" run_guard
assert_eq 0 "$LAST_RC" "--auto pre-wait: in-progress run deferred to after the settle-wait"
assert_contains "$LAST_OUT" "re-checks it after the settle-wait" "deferral is announced"
AUTO_MERGE=true LOOM_DAEMON_BIN="$(make_stub running)" run_guard settled
assert_eq 5 "$LAST_RC" "--auto post-wait: still unconcluded -> HOLD"
assert_not_merged "--auto post-wait unconcluded"
AUTO_MERGE=true LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 1 "$LAST_RC" "--auto pre-wait: a concluded failure still refuses at once"
AUTO_MERGE=true LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 1 "$LAST_RC" "--auto pre-wait: provider failure is not deferred"

echo "Testing --dry-run and forge scoping..."

DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run refusal -> does not exit"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports the would-block"
DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub absent)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run unknown -> does not exit"
assert_contains "$LAST_OUT" "[dry-run] Would HOLD" "--dry-run reports the would-hold, not a silent pass"
DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub old)" run_guard
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports an unavailable gate as a would-block"

FORGE_TYPE=gitea; rm -f "$WORK/argv-cancelled"
LOOM_DAEMON_BIN="$(make_stub cancelled)" run_guard
assert_eq 0 "$LAST_RC" "non-GitHub forge -> gate skipped"
if [[ ! -e "$WORK/argv-cancelled" ]]; then ok "non-GitHub forge -> daemon never invoked"; else fail "non-GitHub forge -> daemon never invoked"; fi
FORGE_TYPE=github

# End-to-end against a REAL daemon build when one exists: the Rust assessor via
# --from-stdin (skipped, not failed, when unbuilt — the Rust unit tests cover it).
REPO_ROOT_DIR="$(cd "$HELPERS_DIR/.." 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
REAL=""
for c in "${LOOM_REAL_DAEMON_BIN:-}" "$REPO_ROOT_DIR/target/debug/loom-daemon"; do
    [[ -n "$c" && -x "$c" ]] && "$c" merge-pr ci-result --help >/dev/null 2>&1 && { REAL="$c"; break; }
done
real() { set +e; out="$(printf '%s' "$1" | "$REAL" merge-pr ci-result --pr 7 --head-sha deadbeef --from-stdin 2>&1)"; rc=$?; set -e; }
if [[ -n "$REAL" ]]; then
    real '{"runs":{"workflow_runs":[{"id":9,"name":"CI","head_sha":"deadbeef","status":"completed","conclusion":"failure","created_at":"2026-10-04T21:00:00Z","html_url":"u"}]},
          "jobs":{"jobs":[{"name":"Detect Changes","conclusion":"cancelled"},{"name":"Structural Checks","conclusion":"success"},{"name":"Daemon Checks","conclusion":"success"},{"name":"Shell Syntax (macos-latest)","conclusion":"success"}]}}'
    assert_eq 1 "$rc" "real daemon: cancelled Detect Changes + green required contexts -> refuse"
    assert_contains "$out" "Detect Changes (cancelled)" "real daemon: names the cancelled job"
    assert_contains "$out" "gh run rerun --failed 9" "real daemon: rerun hint carries the run id"
    real '{"runs":{"workflow_runs":[{"id":9,"name":"CI","head_sha":"deadbeef","status":"completed","conclusion":"success","created_at":"2026-10-04T21:00:00Z"}]}}'
    assert_eq "0 LOOM-CI-RESULT-CLEAN" "$rc $out" "real daemon: exact-head success -> CLEAN"
    real '{"runs":{"workflow_runs":[{"id":9,"name":"CI","head_sha":"0ldhead","status":"completed","conclusion":"success","created_at":"2026-10-04T21:00:00Z"}]},"workflows":{"total_count":1,"workflows":[{"name":"CI"}]}}'
    assert_eq 3 "$rc" "real daemon: green run only for another head -> UNVERIFIED exit 3"
    real '{"runs":{"workflow_runs":[{"id":9,"name":"CI","head_sha":"deadbeef","status":"in_progress","conclusion":null,"created_at":"2026-10-04T21:00:00Z"}]}}'
    assert_eq 3 "$rc" "real daemon: in-progress run -> UNVERIFIED exit 3"
    real '{"runs":{"workflow_runs":[]},"workflows":{"total_count":1,"workflows":[{"name":"Release"}]}}'
    assert_eq 0 "$rc" "real daemon: no CI workflow defined -> exit 0"
    assert_contains "$out" "LOOM-CI-RESULT-NO-CI-WORKFLOW" "real daemon: no-CI is its own sentinel"
    real '{"runs":{"workflow_runs":[]}}'
    assert_eq 2 "$rc" "real daemon: workflow list unreadable -> exit 2 (never a no-CI pass)"
else
    echo "  SKIP: no built loom-daemon with merge-pr ci-result (Rust unit tests cover the assessor)"
fi

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
