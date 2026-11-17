#!/usr/bin/env bash
# test-merge-pr-tree-checks.sh - Unit tests for merge-pr.sh's repo-configured
# pre-merge merge-tree gate (_check_tree_checks, #10026).
#
# The decision/execution is `loom-daemon merge-pr tree-checks` (Rust,
# loom-daemon/src/merge_pr/tree_checks.rs, with its own unit + git-fixture
# tests). This suite pins the merge-pr.sh-side WIRING against a stub binary
# driven through LOOM_DAEMON_BIN: passing tree, failing tree refused (real
# output surfaced, check named), --allow-red-tree bypass, unset config as a
# strict no-op (the daemon is never invoked), fail-closed on a gate that
# cannot run, and --dry-run reporting without blocking.
#
# Usage: ./.loom/scripts/tests/test-merge-pr-tree-checks.sh

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

WORK="$(mktemp -d "${TMPDIR:-/tmp}/test-merge-pr-tree-checks.XXXXXX")"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

FUNCS="$WORK/funcs.sh"
awk '/^_check_tree_checks\(\) \{/ { print; exit }' "$MERGE_PR_SRC" > "$FUNCS"
grep -q '_check_tree_checks()' "$FUNCS" || { echo "FATAL: could not extract _check_tree_checks" >&2; exit 2; }
# shellcheck disable=SC1090
source "$FUNCS"

# Stub daemon: records argv + invocation, then emits the canned outcome.
make_stub() {
    local mode="$1" path; path="$WORK/daemon-$1"
    {
        echo '#!/usr/bin/env bash'
        echo "printf '%s\n' \"\$*\" >> '$WORK/argv-$mode'"
        case "$mode" in
            clean)    echo "echo LOOM-TREE-CHECKS-CLEAN" ;;
            red)      echo "echo 'Merge blocked: PR #7 merge tree fails the tree check \`scripts/check-migration-prefixes.sh\`'; echo 'duplicate prefix 002'; exit 1" ;;
            bypassed) echo "echo 'LOOM-TREE-CHECKS-BYPASSED PR #7: tree check failed; proceeding under --allow-red-tree.'" ;;
            broken)   echo "echo 'tree-checks could not run: fetch failed'; exit 2" ;;
        esac
    } > "$path"
    chmod +x "$path"
    printf '%s' "$path"
}

# A fake repo root: configured and unconfigured variants.
mkdir -p "$WORK/cfg/.loom" "$WORK/empty/.loom" "$WORK/none/.loom"
echo '{"merge":{"treeChecks":["scripts/check-migration-prefixes.sh","npm run typecheck"]}}' > "$WORK/cfg/.loom/config.json"
echo '{"merge":{"treeChecks":[]}}' > "$WORK/empty/.loom/config.json"
echo '{"terminals":[]}' > "$WORK/none/.loom/config.json"

PR_NUMBER=7; PR_JSON='{"base":{"ref":"main"}}'; PR_HEAD_SHA=deadbeef; REPO_NWO=o/r
DRY_RUN=false; ALLOW_RED_TREE=false; REPO_ROOT="$WORK/cfg"
LAST_OUT=""; LAST_RC=0
run_guard() { set +e; LAST_OUT="$(_check_tree_checks 2>&1)"; LAST_RC=$?; set -e; }

echo "Testing _check_tree_checks behavior..."

# T1: passing tree
LOOM_DAEMON_BIN="$(make_stub clean)" run_guard
assert_eq 0 "$LAST_RC" "passing tree -> guard passes"
A="$(cat "$WORK/argv-clean")"
assert_contains "$A" "merge-pr tree-checks --pr 7 --repo o/r --head-sha deadbeef --base-ref main" "daemon gets PR/repo/head/base operands"
# The daemon must read the SAME config the guard just consulted (the main
# checkout's), never the cwd's: run from an issue worktree, the cwd config is
# the PR branch's own copy, which the PR could edit to declare no checks.
assert_contains "$A" "--config $WORK/cfg/.loom/config.json" "daemon reads the guard's config, not the cwd's"

# T2: failing tree refused, real output + failing check named, no bypass flag
LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 1 "$LAST_RC" "failing tree -> merge refused"
assert_contains "$LAST_OUT" "scripts/check-migration-prefixes.sh" "refusal names the failing check"
assert_contains "$LAST_OUT" "duplicate prefix 002" "refusal carries the check's real output"
if grep -q -- "--allow-red-tree" "$WORK/argv-red"; then fail "no bypass flag without --allow-red-tree"; else ok "no bypass flag without --allow-red-tree"; fi

# T3: bypass -> proceeds with a warning, flag threaded to the daemon
ALLOW_RED_TREE=true LOOM_DAEMON_BIN="$(make_stub bypassed)" run_guard
assert_eq 0 "$LAST_RC" "--allow-red-tree -> proceeds"
assert_contains "$LAST_OUT" "WARN: LOOM-TREE-CHECKS-BYPASSED" "bypass is logged as a warning"
assert_contains "$(cat "$WORK/argv-bypassed")" "--allow-red-tree" "flag threaded to the daemon (which records the audit comment)"

# T4: unset / empty config / no config file -> strict no-op, daemon never run
for r in none empty; do
    rm -f "$WORK/argv-red"
    REPO_ROOT="$WORK/$r" LOOM_DAEMON_BIN="$(make_stub red)" run_guard
    assert_eq 0 "$LAST_RC" "config '$r' -> no-op"
    [[ ! -e "$WORK/argv-red" ]] && ok "config '$r' -> daemon never invoked" || fail "config '$r' -> daemon never invoked"
done
REPO_ROOT="$WORK/missing" LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 0 "$LAST_RC" "no config file -> no-op"

# T5: could-not-run (exit 2), missing binary, and exit 0 without the sentinel all fail closed
LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 1 "$LAST_RC" "exit 2 -> refused (fail closed)"
assert_contains "$LAST_OUT" "could not run" "fail-closed path explains itself"
LOOM_DAEMON_BIN="$WORK/does-not-exist" run_guard
assert_eq 1 "$LAST_RC" "missing binary -> refused (fail closed)"
printf '#!/usr/bin/env bash\nexit 0\n' > "$WORK/silent"; chmod +x "$WORK/silent"
LOOM_DAEMON_BIN="$WORK/silent" run_guard
assert_eq 1 "$LAST_RC" "exit 0 without the sentinel -> refused"

# T6: malformed config with the file present reaches the daemon (which fails closed), never a silent pass
echo '{ not json' > "$WORK/none/.loom/config.json"
REPO_ROOT="$WORK/none" LOOM_DAEMON_BIN="$(make_stub broken)" run_guard
assert_eq 1 "$LAST_RC" "malformed config -> daemon consulted, refused"

# T7: dry-run reports the would-block without blocking, and forwards --dry-run (no comments)
DRY_RUN=true LOOM_DAEMON_BIN="$(make_stub red)" run_guard
assert_eq 0 "$LAST_RC" "--dry-run -> does not block"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "--dry-run reports the would-block"
assert_contains "$(tail -1 "$WORK/argv-red")" "--dry-run" "--dry-run forwarded so the daemon posts no comment"

echo
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
