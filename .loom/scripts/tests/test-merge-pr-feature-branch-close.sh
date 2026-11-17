#!/usr/bin/env bash
# test-merge-pr-feature-branch-close.sh - Unit tests for merge-pr.sh's
# _close_branch_derived_issue (#12615).
#
# A `feature/issue-N` PR that merges with no closing keyword (PR #12512 /
# issue #12509) used to leave N open and loom:issue-eligible. merge-pr.sh now
# closes N post-merge unless the PR already closes it or declares it a partial
# increment (`Part of #N` / `Contributes to #N`).
#
# Strategy (as test-merge-pr-closed-issue-cleanup.sh): extract the wrapper
# from merge-pr.sh, source it, stub `gh` on PATH and the one forge helper the
# wrapper asks the shell for (forge_pr_close_targets), and assert on the
# recorded calls. The decision itself is `loom-daemon merge-pr
# post-merge-bookkeeping --step branch-close` (#8191 slice 2), reached for real.
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
# shellcheck source=lib/require-daemon-bin.sh
source "$SCRIPT_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr post-merge-bookkeeping"
# Sets _LOOM_FORGE_HELPERS_LIB_DIR, which the wrapper passes to the verb.
# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

STUB_DIR="$(mktemp -d)"
FUNCS_FILE="$(mktemp)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT

awk '
  /^_post_merge_bookkeeping\(\) \{/ { capture=1 }
  /^_close_branch_derived_issue\(\) \{/ { print; capture=0; next }
  capture { print }
  capture && /^}/ { capture=0 }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"
for _fn in _post_merge_bookkeeping _close_branch_derived_issue; do
    if ! grep -q "^${_fn}() {" "$FUNCS_FILE"; then
        echo -e "${RED}FATAL${NC}: could not extract $_fn" >&2
        exit 2
    fi
done
# shellcheck disable=SC1090
source "$FUNCS_FILE"

cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
# api repos/.../pulls/N/commits -> $LOOM_TEST_COMMITS_JSON (a JSON array)
# api repos/.../issues/N        -> the issue in $LOOM_TEST_ISSUE_STATE
#                                  (open | closed | pr | fail)
# issue close ...               -> recorded
LOG="$LOOM_TEST_STUB_DIR/gh-calls.log"
case "$1" in
  api)
    case "$2" in
      */commits) printf '%s\n' "${LOOM_TEST_COMMITS_JSON:-[]}"; exit 0 ;;
      */issues/*)
        case "${LOOM_TEST_ISSUE_STATE:-open}" in
          fail) exit 1 ;;
          pr) echo '{"state":"open","pull_request":{"url":"x"}}' ;;
          *) printf '{"state":"%s"}\n' "${LOOM_TEST_ISSUE_STATE:-open}" ;;
        esac
        exit 0 ;;
    esac ;;
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
forge_pr_close_targets() { printf '%s' "$FORGE_CLOSE_TARGETS"; }

reset() {
    : > "$STUB_DIR/gh-calls.log"
    FORGE_CLOSE_TARGETS=""
    unset LOOM_TEST_COMMITS_JSON
    PR_BRANCH="feature/issue-42" FORGE_TYPE="github"
    PR_JSON='{"body":"Summary only"}'
    export LOOM_TEST_ISSUE_STATE=open
}
calls() { cat "$STUB_DIR/gh-calls.log"; }

echo "=== _close_branch_derived_issue (#12615) ==="

reset; _close_branch_derived_issue >/dev/null
assert_contains "$(calls)" "issue close 42" "no keyword, no partial -> issue closed"
assert_contains "$(calls)" "--reason completed" "closed as completed"

reset; PR_JSON='{"body":"Part of #42"}'; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "Part of #42 -> left open"

reset; PR_JSON='{"body":"Part of #7"}'; _close_branch_derived_issue >/dev/null
assert_contains "$(calls)" "issue close 42" "partial ref to a different issue does not protect #42"

reset; FORGE_CLOSE_TARGETS="42"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "already a close target -> no behaviour change"

reset; PR_JSON='{"body":"Closes #42"}'; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "closing keyword in body (targets lookup empty) -> not closed here"

reset; export LOOM_TEST_COMMITS_JSON='[{"commit":{"message":"fix: thing\n\nFixes #42"}}]'; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "closing keyword in a commit message -> not closed here"

echo "  RETIRED: 'partial analysis failed -> fail safe, nothing closed'"
echo "      property:   when the partial-increment analysis cannot run, nothing is closed"
echo "      structural: the analysis is an in-process call (merge_pr::refs::partial_increment_refs), not a \$(...) capture that can fail; there is no seam to fail"
echo "      successor:  a verb that did not run at all closes nothing (T11/T12 in test-merge-pr-closed-issue-cleanup.sh drive the same wrapper) and post_merge_bookkeeping::tests::branch_close_skips_each_reason_to_leave_it_open"
TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))

reset; LOOM_TEST_ISSUE_STATE="closed"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "issue already closed -> no-op"

reset; LOOM_TEST_ISSUE_STATE="pr"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "number is a PR -> no-op"

reset; LOOM_TEST_ISSUE_STATE="fail"; _close_branch_derived_issue >/dev/null
assert_eq "" "$(calls)" "issue unreadable -> nothing closed on no evidence"

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
