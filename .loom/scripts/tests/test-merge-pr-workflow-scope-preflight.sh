#!/usr/bin/env bash
# test-merge-pr-workflow-scope-preflight.sh - merge-pr.sh's pre-merge
# `workflow` token-scope guard (#10539).
#
# GitHub 403s a merge of a PR touching .github/workflows/* when the gh OAuth
# token lacks the `workflow` scope. The guard is `loom-daemon merge-pr
# workflow-scope` (decision logic unit-tested in Rust); this suite drives the
# merge-pr.sh wrapper `_check_workflow_scope` (extracted from the real source)
# against the REAL daemon binary with a stubbed `gh`, asserting:
#   (a) workflow file + scopes lacking `workflow` -> blocked, names the fix
#   (b) workflow file + `workflow` scope present  -> proceeds
#   (c) no workflow files                         -> no scope lookup, proceeds
#   (d) no X-OAuth-Scopes header (App token)      -> proceeds
#   (e) lookup failure                            -> proceeds (fail open)
# plus --dry-run reporting and the env escape hatch.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-workflow-scope-preflight.sh

# SC2034: globals are read only by the extracted+sourced function.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_PASSED=0; TESTS_FAILED=0

_pass() { TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
_fail() { TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; shift; printf '    %s\n' "$@"; }

assert_eq() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if [[ "$1" == "$2" ]]; then _pass "$3"; else _fail "$3" "Expected: '$1'" "Actual:   '$2'"; fi
}
assert_contains() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if grep -qF -- "$2" <<<"$1"; then _pass "$3"; else _fail "$3" "Expected substring: '$2'" "In: '$1'"; fi
}
assert_not_contains() {
    TESTS_RUN=$((TESTS_RUN + 1))
    if ! grep -qF -- "$2" <<<"$1"; then _pass "$3"; else _fail "$3" "Unexpected substring: '$2'" "In: '$1'"; fi
}

info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

WORK="$(mktemp -d)"
FUNCS_FILE="$WORK/funcs.sh"
trap 'rm -rf "$WORK" 2>/dev/null || true' EXIT

# Extract the single-line wrapper (and the roll-hint helper it may not need).
awk '
  /^_check_workflow_scope\(\) \{/ { print; exit }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"
if ! grep -q '_check_workflow_scope()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_workflow_scope from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- gh stub: behaviour driven by env, every call logged ---
mkdir -p "$WORK/bin"
cat > "$WORK/bin/gh" <<'STUB'
#!/usr/bin/env bash
echo "$*" >> "$STUB_LOG"
case "$*" in
  *"/files"*)
    [[ "${STUB_FAIL_FILES:-0}" != "1" ]] || { echo "boom" >&2; exit 1; }
    printf '%b' "${STUB_FILES:-}" ;;
  "api -i user")
    [[ "${STUB_FAIL_USER:-0}" != "1" ]] || { echo "rate limited" >&2; exit 1; }
    printf 'HTTP/2.0 200 OK\r\nContent-Type: application/json\r\n'
    [[ -z "${STUB_SCOPES+x}" ]] || printf 'X-Oauth-Scopes: %s\r\n' "$STUB_SCOPES"
    printf '\r\n{"login":"op"}\n' ;;
  *) exit 1 ;;
esac
STUB
chmod +x "$WORK/bin/gh"
export PATH="$WORK/bin:$PATH" LOOM_GH_BIN="$WORK/bin/gh" STUB_LOG="$WORK/gh.log"

# shellcheck disable=SC2034  # read by the extracted function
PR_NUMBER=2022; REPO_NWO="owner/repo"; FORGE_TYPE=github; DRY_RUN=false
LAST_OUT=""; LAST_RC=0
run_guard() {
    : > "$STUB_LOG"
    set +e
    LAST_OUT="$(_check_workflow_scope 2>&1)"
    LAST_RC=$?
    set -e
}
reset_stub() { unset STUB_FILES STUB_SCOPES STUB_FAIL_FILES STUB_FAIL_USER LOOM_SKIP_WORKFLOW_SCOPE_CHECK; }
WF='README.md\n.github/workflows/deploy.yml\n'
FIX='gh auth refresh -h github.com -s workflow'

echo "Testing _check_workflow_scope behavior..."

# (a) workflow file, scopes lack `workflow` -> blocked, names the fix.
reset_stub; export STUB_FILES="$WF" STUB_SCOPES="admin:public_key, gist, read:org, repo"
run_guard
assert_eq "1" "$LAST_RC" "(a) workflow PR + token lacking scope -> exit 1 before merge"
assert_contains "$LAST_OUT" "$FIX" "(a) message names the fix command"
assert_contains "$LAST_OUT" "#2022" "(a) message names the PR"

# (a2) dry-run reports without blocking.
DRY_RUN=true; run_guard; DRY_RUN=false
assert_eq "0" "$LAST_RC" "(a2) --dry-run does not exit non-zero"
assert_contains "$LAST_OUT" "[dry-run] Would BLOCK" "(a2) --dry-run reports the would-be block"

# (b) scope present -> proceeds.
reset_stub; export STUB_FILES="$WF" STUB_SCOPES="repo, workflow"
run_guard
assert_eq "0" "$LAST_RC" "(b) workflow PR + workflow scope -> proceeds"
assert_eq "" "$LAST_OUT" "(b) silent"

# (c) no workflow files -> no scope lookup at all.
reset_stub; export STUB_FILES='README.md\nsrc/x.rs\n' STUB_SCOPES="repo"
run_guard
assert_eq "0" "$LAST_RC" "(c) no workflow files -> proceeds"
assert_not_contains "$(cat "$STUB_LOG")" "api -i user" "(c) no scope lookup performed"

# (d) no scopes header (App / fine-grained token) -> proceeds.
reset_stub; export STUB_FILES="$WF"
run_guard
assert_eq "0" "$LAST_RC" "(d) missing X-OAuth-Scopes header -> proceeds"

# (e) lookup failures -> proceed (fail open).
reset_stub; export STUB_FILES="$WF" STUB_SCOPES="repo" STUB_FAIL_USER=1
run_guard
assert_eq "0" "$LAST_RC" "(e) scope lookup failure -> proceeds"
reset_stub; export STUB_FAIL_FILES=1 STUB_SCOPES="repo"
run_guard
assert_eq "0" "$LAST_RC" "(e) file-list lookup failure -> proceeds"

# Escape hatch.
reset_stub; export STUB_FILES="$WF" STUB_SCOPES="repo" LOOM_SKIP_WORKFLOW_SCOPE_CHECK=1
run_guard
assert_eq "0" "$LAST_RC" "LOOM_SKIP_WORKFLOW_SCOPE_CHECK=1 bypasses the guard"

echo ""
echo "Tests run: $TESTS_RUN, passed: $TESTS_PASSED, failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]]
