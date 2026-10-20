#!/usr/bin/env bash
# test-merge-pr-feature-branch-close.sh - Unit tests for merge-pr.sh's
# _close_branch_derived_issue (#12615).
#
# A `feature/issue-N` PR that merges with no closing keyword (PR #12512 /
# issue #12509) used to leave N open and loom:issue-eligible. merge-pr.sh now
# closes N post-merge unless the PR already closes it or declares it a partial
# increment (`Part of #N` / `Contributes to #N`).
#
# Strategy (as test-merge-pr-closed-issue-cleanup.sh): extract the function
# from merge-pr.sh, source it, stub `gh` on PATH and the helpers it calls, and
# assert on the recorded calls.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-feature-branch-close.sh

# SC2034: globals are read only by the extracted function.
# shellcheck disable=SC2034

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# shellcheck source=lib/assert.sh
source "$SCRIPT_DIR/lib/assert.sh"

info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

STUB_DIR="$(mktemp -d)"
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT

awk '
  /^_close_branch_derived_issue\(\) \{/ { capture=1 }
  capture { print }
  capture && /^}/ { capture=0 }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"
if ! grep -q '^_close_branch_derived_issue() {' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _close_branch_derived_issue" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# api repos/.../issues/N -> $LOOM_TEST_ISSUE_STATE (the --jq result)
# issue close ...        -> recorded
LOG="$LOOM_TEST_STUB_DIR/gh-calls.log"
case "$1" in
  api) printf '%s\n' "${LOOM_TEST_ISSUE_STATE:-open}"; exit 0 ;;
  issue) echo "$*" >> "$LOG"; exit 0 ;;
esac
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR" PATH="$STUB_DIR:$PATH"

REPO_NWO="owner/repo"
PR_NUMBER="999"
FORGE_TYPE="github"
GH="gh"
PR_JSON='{"body":""}'
PR_BRANCH="feature/issue-42"

FORGE_CLOSE_TARGETS=""
PARTIAL_REFS=""
PARTIAL_FAIL=0
COMMIT_MSGS=""
forge_pr_close_targets() { printf '%s' "$FORGE_CLOSE_TARGETS"; }
_partial_increment_refs() { [[ $PARTIAL_FAIL -eq 0 ]] || return 1; printf '%s' "$PARTIAL_REFS"; }
_pr_commit_messages() { printf '%s' "$COMMIT_MSGS"; }

reset() {
    : > "$STUB_DIR/gh-calls.log"
    FORGE_CLOSE_TARGETS="" PARTIAL_REFS="" PARTIAL_FAIL=0 COMMIT_MSGS=""
    PR_BRANCH="feature/issue-42" FORGE_TYPE="github"
    PR_JSON='{"body":"Summary only"}'
    export LOOM_TEST_ISSUE_STATE=open
}
calls() { cat "$STUB_DIR/gh-calls.log"; }

echo "=== _close_branch_derived_issue (#12615) ==="

reset; _close_branch_derived_issue >/dev/null
assert_contains "$(calls)" "issue close 42" "no keyword, no partial -> issue closed"
assert_contains "$(calls)" "--reason completed" "closed as completed"

reset; PARTIAL_REFS="42"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "Part of #42 -> left open"

reset; PARTIAL_REFS="7"; _close_branch_derived_issue >/dev/null
assert_contains "$(calls)" "issue close 42" "partial ref to a different issue does not protect #42"

reset; FORGE_CLOSE_TARGETS="42"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "already a close target -> no behaviour change"

reset; PR_JSON='{"body":"Closes #42"}'; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "closing keyword in body (targets lookup empty) -> not closed here"

reset; COMMIT_MSGS="fix: thing

Fixes #42"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "closing keyword in a commit message -> not closed here"

reset; PARTIAL_FAIL=1; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "partial analysis failed -> fail safe, nothing closed"

reset; LOOM_TEST_ISSUE_STATE="closed"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "issue already closed -> no-op"

reset; LOOM_TEST_ISSUE_STATE="pr"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "number is a PR -> no-op"

reset; PR_BRANCH="feature/issue-42-install-merge"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "suffix branch not matched by exact pattern"

reset; PR_BRANCH="fix/something"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "non-issue branch -> no-op"

reset; FORGE_TYPE="gitea"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "gitea -> no-op (GitHub only)"

src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_close_branch_derived_issue || true" \
  "merge-pr.sh invokes the branch-derived close at the confirmed-merge choke point"

echo ""
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"
[[ $TESTS_FAILED -eq 0 ]]
