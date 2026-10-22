#!/usr/bin/env bash
# test-merge-pr-required-checks.sh - Tests for merge-pr.sh's required-context
# conclusion gate on the plain (non --auto) path (_check_required_checks, #10688).
#
# The decision is `loom-daemon merge-pr required-checks` (Rust,
# loom-daemon/src/merge_pr/checks_failure/required.rs, with its own unit
# tests). This suite pins the merge-pr.sh WIRING against a stubbed forge: a fake
# native binary (LOOM_DAEMON_BIN) and a fake forge merge call (forge_merge_pr,
# recorded to a file). Every case runs the gate and then "the merge", exactly
# as the script does, and asserts the merge is reached only when it should be.
#
#   CLEAN                                          -> merges
#   red required context outside `CI`, exit 1      -> exit 1, never merges, names it
#   missing / pending required context, exit 3     -> HOLD exit 5, never merges
#   forge unreadable, exit 2                       -> exit 1, never merges
#   --allow-red-required <ctx> (repeatable)        -> forwarded per context; BYPASSED merges
#   --dry-run                                      -> Would BLOCK / Would HOLD, merges nothing
#   a daemon predating the verb                    -> warns, merges (fail-open; no floor raise)
#   --auto / non-GitHub forge                      -> gate not run
#
# When a built loom-daemon carrying the verb exists, the same wiring is driven
# end to end against the REAL assessor (--from-stdin fixtures), including
# "overriding one context does not let a different red context through".
#
# Usage: ./.loom/scripts/tests/test-merge-pr-required-checks.sh

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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-required-checks.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

FUNCS="$WORK/funcs.sh"
# A one-liner in merge-pr.sh.
awk '/^_check_required_checks\(\) \{/ { print }' "$MERGE_PR_SRC" > "$FUNCS"
grep -q '^_check_required_checks() {.*; }$' "$FUNCS" || { echo "FATAL: could not extract _check_required_checks" >&2; exit 2; }
# shellcheck disable=SC1090
source "$FUNCS"

# --- Structural wiring ---
echo "Testing _check_required_checks wiring..."
call_line="$(awk '/^unset _MPS_JSON _MPS_FRESH_SHA; \[\[ "\$AUTO_MERGE" == "true" \]\] \|\| _check_required_checks$/ { print NR; exit }' "$MERGE_PR_SRC")"
merge_line="$(awk '/forge_merge_pr "\$REPO_NWO" "\$PR_NUMBER" "\$MERGE_PRECONDITION_SHA"/ { print NR; exit }' "$MERGE_PR_SRC")"
auto_line="$(awk '/^if \[\[ "\$AUTO_MERGE" == "true" \]\]; then$/ { print NR; exit }' "$MERGE_PR_SRC")"
if [[ -n "$call_line" && -n "$merge_line" && "$call_line" -lt "$merge_line" ]]; then
    ok "gate runs (non --auto only) before the forge merge call"
else
    fail "gate runs (non --auto only) before the forge merge call" "call=$call_line merge=$merge_line"
fi
if [[ -n "$call_line" && -n "$auto_line" && "$call_line" -lt "$auto_line" ]]; then
    ok "gate is decided before --auto flips AUTO_MERGE=false (the --auto path is unchanged)"
else
    fail "gate is decided before --auto flips AUTO_MERGE=false" "call=$call_line auto=$auto_line"
fi
if awk '/^_revalidate_merge_guards\(\) \{/ { inf=1 } inf && /_check_required_checks/ { found=1 } inf && /^\}/ { exit } END { exit found }' "$MERGE_PR_SRC"; then
    ok "--auto's post-wait revalidation does not call the gate"
else
    fail "--auto's post-wait revalidation does not call the gate"
fi
assert_contains "$(grep -- '--allow-red-required)' "$MERGE_PR_SRC")" 'ALLOW_RED_REQUIRED+=("$2")' "--allow-red-required is repeatable (appends)"
assert_contains "$(sed -n '1,/^set -euo pipefail/p' "$MERGE_PR_SRC")" "--allow-red-required <context>" "flag documented in the merge-pr.sh header"
assert_contains "$(cat "$HELPERS_DIR/../docs/merge-pr-exit-code-exceptions.md")" "--allow-red-required <context>" "flag documented in merge-pr-exit-code-exceptions.md"

# --- Fake native binary ---
make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" >> '$WORK/argv-$mode'"
        case "$mode" in
            clean)    echo "echo LOOM-REQUIRED-CHECKS-CLEAN" ;;
            red)      echo "echo 'Merge blocked: PR #7'\"'\"'s head deadbeef has required status-check context(s) that concluded red: \`GitHub Bypass Check\` (failure) (#10688).'; exit 1" ;;
            missing)  echo "echo 'LOOM-REQUIRED-CHECKS-HOLD required status-check context(s) not concluded green on head deadbeef: \`GitHub Bypass Check\` (not reported) (#10688).'; exit 3" ;;
            pending)  echo "echo 'LOOM-REQUIRED-CHECKS-HOLD required status-check context(s) not concluded green on head deadbeef: \`GitHub Bypass Check\` (in_progress) (#10688).'; exit 3" ;;
            broken)   echo "echo 'LOOM-REQUIRED-CHECKS-UNREADABLE ruleset lookup failed: HTTP 502'; exit 2" ;;
            bypassed) echo "echo 'LOOM-REQUIRED-CHECKS-BYPASSED PR #7: red required context(s) \`A\` (failure) overridden by --allow-red-required (#10688).'" ;;
            old)      echo "echo \"error: unrecognized subcommand 'required-checks'\" >&2; exit 2" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

forge_merge_pr() { echo "$*" >> "$WORK/merge-called"; }

PR_NUMBER=7; PR_HEAD_SHA=deadbeef; REPO_NWO=o/r; FORGE_TYPE=github; DRY_RUN=false
PR_JSON='{"base":{"ref":"main"}}'; MERGE_PRECONDITION_SHA=deadbeef; ALLOW_RED_REQUIRED=()
LAST_OUT=""; LAST_RC=0
run_guard() {
    rm -f "$WORK/merge-called"
    set +e; LAST_OUT="$( (_check_required_checks; forge_merge_pr "$REPO_NWO" "$PR_NUMBER" "$MERGE_PRECONDITION_SHA" squash) 2>&1)"; LAST_RC=$?; set -e
}
merged() { [[ -e "$WORK/merge-called" ]]; }
assert_merged()     { if merged; then ok "$1 -> merge call made"; else fail "$1 -> merge call made"; fi; }
assert_not_merged() { if merged; then fail "$1 -> merge call NEVER made"; else ok "$1 -> merge call NEVER made"; fi; }

echo "Testing _check_required_checks verdicts..."

LOOM_DAEMON_BIN="$(make_stub clean)" run_guard
assert_eq 0 "$LAST_RC" "every required context green -> passes"
assert_merged "CLEAN"
assert_contains "$(cat "$WORK/argv-clean")" "merge-pr required-checks --pr 7 --repo o/r --head-sha deadbeef --base-ref main" "daemon gets PR/repo/head/base operands"
assert_not_contains "$(cat "$WORK/argv-clean")" "--dry-run" "a real run does not pass --dry-run (so the daemon may post the audit comment)"

LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 1 "$LAST_RC" "red required context outside CI -> refused (exit 1)"
assert_not_merged "red required context"
assert_contains "$LAST_OUT" "GitHub Bypass Check" "refusal names the context"

for mode in missing pending; do
    LOOM_DAEMON_BIN="$(make_stub "$mode")" run_guard
    assert_eq 5 "$LAST_RC" "$mode required context -> HOLD, exit 5"
    assert_not_merged "$mode required context"
    assert_contains "$LAST_OUT" "Merge held: PR #7" "$mode hold names the PR"
    assert_contains "$LAST_OUT" "GitHub Bypass Check" "$mode hold names the context"
done

LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 1 "$LAST_RC" "forge unreadable -> refused"
assert_not_merged "forge unreadable"
assert_contains "$LAST_OUT" "HTTP 502" "refusal quotes the read failure"

ALLOW_RED_REQUIRED=("A" "Lint / ubuntu (1)")
LOOM_DAEMON_BIN="$(make_stub bypassed)" run_guard
assert_eq 0 "$LAST_RC" "--allow-red-required override -> passes"
assert_merged "BYPASSED"
assert_contains "$LAST_OUT" "overridden by --allow-red-required" "override is announced, not silent"
assert_contains "$(cat "$WORK/argv-bypassed")" "--allow-red-required A --allow-red-required Lint / ubuntu (1)" "every named context forwarded, one flag each"
ALLOW_RED_REQUIRED=()

LOOM_DAEMON_BIN="$(make_stub old)" run_guard
assert_eq 0 "$LAST_RC" "daemon predating the verb -> fail-open (branch protection stays the guard)"
assert_merged "old daemon"
assert_contains "$LAST_OUT" "did not run" "fail-open is announced, not silent"

echo "Testing --dry-run, --auto and forge scoping..."

DRY_RUN=true; rm -f "$WORK/argv-red"
LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run red -> does not exit"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports the would-block"
assert_contains "$LAST_OUT" "GitHub Bypass Check" "--dry-run block names the context"
assert_contains "$(cat "$WORK/argv-red")" "--dry-run" "--dry-run forwarded (the daemon posts no comment)"
LOOM_DAEMON_BIN="$(make_stub pending)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run pending -> does not exit"
assert_contains "$LAST_OUT" "[dry-run] Would HOLD" "--dry-run reports the would-hold"
assert_contains "$LAST_OUT" "GitHub Bypass Check" "--dry-run hold names the context"
DRY_RUN=false

FORGE_TYPE=gitea; rm -f "$WORK/argv-red"
LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 0 "$LAST_RC" "non-GitHub forge -> gate skipped"
if [[ ! -e "$WORK/argv-red" ]]; then ok "non-GitHub forge -> daemon never invoked"; else fail "non-GitHub forge -> daemon never invoked"; fi
FORGE_TYPE=github

# --- End to end against a REAL daemon build, when one exists ---
REPO_ROOT_DIR="$(cd "$HELPERS_DIR/.." 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
REAL=""
for c in "${LOOM_REAL_DAEMON_BIN:-}" "${CARGO_TARGET_DIR:+$CARGO_TARGET_DIR/debug/loom-daemon}" "$REPO_ROOT_DIR/target/debug/loom-daemon"; do
    [[ -n "$c" && -x "$c" ]] && "$c" merge-pr required-checks --help >/dev/null 2>&1 && { REAL="$c"; break; }
done
# real_wired <fixture-json> — the shell gate, with the daemon reading the fixture instead of the forge.
real_wired() {
    printf '%s' "$1" > "$WORK/fixture.json"
    printf '#!/usr/bin/env bash\nexec %q "$@" --from-stdin < %q\n' "$REAL" "$WORK/fixture.json" > "$WORK/daemon-real"
    chmod +x "$WORK/daemon-real"
    LOOM_DAEMON_BIN="$WORK/daemon-real" run_guard
}
if [[ -n "$REAL" ]]; then
    echo "Testing against the real assessor ($REAL)..."
    RUNS='[{"name":"CI Result","status":"completed","conclusion":"success","started_at":"2026-10-11T10:00:00Z"},
           {"name":"GitHub Bypass Check","status":"completed","conclusion":"failure","started_at":"2026-10-11T10:00:00Z"},
           {"name":"Other Gate","status":"completed","conclusion":"cancelled","started_at":"2026-10-11T10:00:00Z"}]'
    real_wired "{\"required\":[\"CI Result\",\"GitHub Bypass Check\"],\"check_runs\":$RUNS}"
    assert_eq 1 "$LAST_RC" "real: red required check outside CI (CI Result green) -> exit 1"
    assert_not_merged "real: red required"
    assert_contains "$LAST_OUT" "\`GitHub Bypass Check\` (failure)" "real: refusal names the red context"
    ALLOW_RED_REQUIRED=("GitHub Bypass Check")
    real_wired "{\"required\":[\"CI Result\",\"GitHub Bypass Check\"],\"check_runs\":$RUNS}"
    assert_eq 0 "$LAST_RC" "real: --allow-red-required on the red context -> merges"
    assert_merged "real: overridden"
    real_wired "{\"required\":[\"CI Result\",\"GitHub Bypass Check\",\"Other Gate\"],\"check_runs\":$RUNS}"
    assert_eq 1 "$LAST_RC" "real: overriding one context does not admit a different red one"
    assert_not_merged "real: second red context"
    assert_contains "$LAST_OUT" "\`Other Gate\` (cancelled)" "real: refusal names the un-overridden context"
    assert_not_contains "$LAST_OUT" "\`GitHub Bypass Check\` (failure)" "real: refusal omits the overridden context"
    ALLOW_RED_REQUIRED=()
    real_wired "{\"required\":[\"CI Result\",\"Absent\"],\"check_runs\":$RUNS}"
    assert_eq 5 "$LAST_RC" "real: missing required context -> HOLD exit 5"
    assert_contains "$LAST_OUT" "\`Absent\` (not reported)" "real: hold names the missing context"
    real_wired '{"required":["ext/ci"],"check_runs":[],"statuses":{"total_count":1,"statuses":[{"context":"ext/ci","state":"pending"}]}}'
    assert_eq 5 "$LAST_RC" "real: pending commit status -> HOLD exit 5"
    real_wired '{"required":["ext/ci"],"check_runs":[],"statuses":{"total_count":1,"statuses":[{"context":"ext/ci","state":"success"}]}}'
    assert_eq 0 "$LAST_RC" "real: green commit status -> merges"
    real_wired '{"required":["ext/ci"],"check_runs":[],"statuses":{"total_count":3,"statuses":[]}}'
    assert_eq 1 "$LAST_RC" "real: truncated status read -> refused, never a pass"
    DRY_RUN=true
    real_wired "{\"required\":[\"GitHub Bypass Check\"],\"check_runs\":$RUNS}"
    assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "real: --dry-run reports Would BLOCK"
    DRY_RUN=false
else
    echo "  SKIP: no built loom-daemon with merge-pr required-checks (Rust unit tests cover the assessor)"
fi

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
