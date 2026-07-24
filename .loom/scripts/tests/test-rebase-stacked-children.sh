#!/usr/bin/env bash
# test-rebase-stacked-children.sh - Unit tests for rebase-stacked-children.sh
# (#3747, stacked-PR v2 item 3: rebase-on-parent-amend).
#
# When a stacked PARENT branch (feature/issue-<N>) is amended/pushed while still
# open under review, any CHILD PR that branched off its pre-amend tip is stale.
# rebase-stacked-children.sh discovers open CHILD PRs based on the parent branch
# via a LIVE forge query (`gh pr list --base <parent>`, never the daemon
# registry) and, per child:
#   - Up to date (parent tip is already an ancestor): no-op, skip.
#   - Stale + safe   (child issue NOT loom:building): rebase onto the parent's
#     current tip + push --force-with-lease. NO PR base retarget.
#   - Stale + unsafe (child issue still loom:building): skip the rebase and post
#     a deferred-rebase comment on the child PR instead.
# It is a no-op for non-feature/issue-N parent branches and non-GitHub forges,
# and --dry-run reports the per-child outcome without any git/gh mutation.
#
# Strategy (mirrors test-merge-pr-auto-reconcile.sh): the functions under test
# (_rebase_stacked_children, _process_one_stacked_child, run) depend only on
# globals (FORGE_TYPE, REPO_NWO, DRY_RUN, RSC_FAILURE) plus the `gh` and `git`
# CLIs. We extract the function definitions from rebase-stacked-children.sh and
# source them, stub `gh` + `git` on PATH to serve canned data and record mutating
# calls, then assert on the recorded calls. Extracting from source (rather than
# replicating) keeps the test in lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-rebase-stacked-children.sh

# SC2034: several globals (FORGE_TYPE, REPO_NWO, DRY_RUN, RSC_FAILURE) are read
# only by the functions extracted+sourced from rebase-stacked-children.sh, which
# the linter cannot see — every such assignment looks "unused" to it.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
RSC_SRC="$HELPERS_DIR/rebase-stacked-children.sh"

# Colors (YELLOW/BLUE are referenced by the extracted run()/info shims under set -u).
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
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
    if printf '%s' "$haystack" | grep -qF -- "$needle"; then
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
    if ! printf '%s' "$haystack" | grep -qF -- "$needle"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging shims the extracted functions call ---
err()  { echo "ERR: $*" >&2; }
ok()   { echo "OK: $*"; }
info() { echo "INFO: $*"; }
warn() { echo "WARN: $*" >&2; }

# --- Extract the functions under test from rebase-stacked-children.sh ---
# From `run() {` up to (not including) the `# ---- main ----` sentinel. This span
# holds run(), _process_one_stacked_child(), and _rebase_stacked_children().
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" 2>/dev/null || true' EXIT
awk '
  /^run\(\) \{/       { capture=1 }
  /^# ---- main ----/ { capture=0 }
  capture { print }
' "$RSC_SRC" > "$FUNCS_FILE"

if ! grep -q '_rebase_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _rebase_stacked_children from $RSC_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
#   gh api repos/OWNER/REPO/issues/N   -> cat $STUB_DIR/issue-N.json (or {})
#   gh pr list --base B ...            -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
#   gh pr comment N ...                -> record to $STUB_DIR/gh-calls.log
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  path="${!#}"
  num="${path##*/}"
  canned="$STUB_DIR_FROM_ENV/issue-$num.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '{}'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "list" ]]; then
  base=""
  shift 2
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--base" ]]; then base="$2"; shift 2; continue; fi
    shift
  done
  safe="${base//\//_}"
  canned="$STUB_DIR_FROM_ENV/prlist-$safe.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"

# --- Stub git on PATH ---
#   git fetch ...                          -> record + exit 0
#   git merge-base --is-ancestor A <child> -> exit code from $STUB_DIR/ancestor-<safe child>
#                                             (file present -> its value; absent -> 1 = stale)
#   git rebase --abort                     -> record + exit 0
#   git rebase A B                         -> record + exit $LOOM_TEST_REBASE_EXIT (default 0)
#   git push ...                           -> record + exit $LOOM_TEST_PUSH_EXIT (default 0)
#   (anything else)                        -> record + exit 0
cat > "$STUB_DIR/git" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub git: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/git-calls.log"
echo "git $*" >> "$LOG"
case "$1" in
  fetch) exit 0 ;;
  merge-base)
    # merge-base --is-ancestor origin/<parent> origin/<child>; last arg = child ref.
    child="${!#}"
    safe="${child//\//_}"
    f="$STUB_DIR_FROM_ENV/ancestor-$safe"
    if [[ -f "$f" ]]; then exit "$(cat "$f")"; else exit 1; fi
    ;;
  rebase)
    if [[ "$2" == "--abort" ]]; then exit 0; fi
    exit "${LOOM_TEST_REBASE_EXIT:-0}"
    ;;
  push) exit "${LOOM_TEST_PUSH_EXIT:-0}" ;;
  *) exit 0 ;;
esac
STUB
chmod +x "$STUB_DIR/git"

export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

# --- Shared globals the functions read (see the file-level SC2034 disable). ---
REPO_NWO="owner/repo"
FORGE_TYPE="github"
DRY_RUN=false
RSC_FAILURE=0

# Canned issue fixtures (child issue label state).
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-202.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Fixture writers.
write_prlist()   { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist()   { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }
mark_uptodate()  { echo 0 > "$STUB_DIR/ancestor-origin_${1//\//_}"; }  # parent tip IS ancestor
clear_uptodate() { rm -f "$STUB_DIR/ancestor-origin_${1//\//_}"; }     # default: stale (exit 1)

reset_state() {
    : > "$STUB_DIR/gh-calls.log"
    : > "$STUB_DIR/git-calls.log"
    DRY_RUN=false
    RSC_FAILURE=0
    unset LOOM_TEST_REBASE_EXIT
    unset LOOM_TEST_PUSH_EXIT
}
read_gh()  { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }
read_git() { cat "$STUB_DIR/git-calls.log" 2>/dev/null || true; }

echo "Testing _rebase_stacked_children behavior..."

# (a) No open children -> no-op (no rebase, no comment).
reset_state
clear_prlist "feature/issue-100"     # stub returns [] with no fixture
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(a) No open children -> no rebase attempted"
assert_eq "" "$(read_gh)" "(a) No open children -> no comment posted"

# (b) One child already up to date (parent tip is an ancestor) -> no-op.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
mark_uptodate "feature/issue-201"
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(b) Up-to-date child -> no rebase attempted"
assert_eq "" "$(read_gh)" "(b) Up-to-date child -> no comment posted"
clear_uptodate "feature/issue-201"

# (c) One stale, safe child (issue 201 not loom:building) -> rebase + push,
#     no PR base retarget (no `gh pr edit`), no deferred comment.
reset_state
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_contains "$(read_git)" "git rebase origin/feature/issue-100 feature/issue-201" \
  "(c) Safe stale child -> rebased onto origin/feature/issue-100"
assert_contains "$(read_git)" "git push --force-with-lease" \
  "(c) Safe stale child -> pushed with --force-with-lease"
assert_not_contains "$(read_gh)" "pr edit" "(c) Safe stale child -> PR base NOT retargeted"
assert_eq "" "$(read_gh)" "(c) Safe stale child -> no deferred comment"
assert_eq "0" "$RSC_FAILURE" "(c) Safe stale child -> RSC_FAILURE stays 0"

# (d) One stale, unsafe child (issue 202 loom:building) -> deferred comment, no rebase.
reset_state
write_prlist "feature/issue-100" '[{"number":502,"headRefName":"feature/issue-202"}]'
clear_uptodate "feature/issue-202"   # stale
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(d) Unsafe stale child -> rebase NOT attempted"
assert_contains "$(read_gh)" "pr comment 502 --repo owner/repo" \
  "(d) Unsafe stale child -> deferred-rebase comment posted on PR #502"

# (e) Non-feature/issue-N parent branch -> script skips entirely (no discovery).
reset_state
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
_rebase_stacked_children "release-1"
assert_not_contains "$(read_git)" "rebase" "(e) Non-feature/issue-N parent -> no rebase"
assert_eq "" "$(read_gh)" "(e) Non-feature/issue-N parent -> no comment, no discovery"

# (e2) FORGE_TYPE != github -> no-op (GitHub-only).
reset_state
FORGE_TYPE="gitea"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "rebase" "(e2) FORGE_TYPE=gitea -> no rebase (GitHub-only)"
assert_eq "" "$(read_gh)" "(e2) FORGE_TYPE=gitea -> no comment"
FORGE_TYPE="github"

# (f) --dry-run with a stale safe child -> reports would-be rebase without executing.
reset_state
DRY_RUN=true
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_not_contains "$(read_git)" "git rebase origin/feature/issue-100 feature/issue-201" \
  "(f) Dry-run stale safe child -> rebase NOT executed"
assert_not_contains "$(read_git)" "git push --force-with-lease" \
  "(f) Dry-run stale safe child -> push NOT executed"
assert_eq "" "$(read_gh)" "(f) Dry-run stale safe child -> no comment posted"
DRY_RUN=false

# (g bonus) Rebase conflict on the safe path -> RSC_FAILURE=2, abort recorded,
#           run continues (does not tear down the process).
reset_state
export LOOM_TEST_REBASE_EXIT=1
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_uptodate "feature/issue-201"   # stale
_rebase_stacked_children "feature/issue-100"
assert_eq "2" "$RSC_FAILURE" "(g) Rebase conflict -> RSC_FAILURE=2 (exit 2 at end)"
assert_contains "$(read_git)" "git rebase --abort" \
  "(g) Rebase conflict -> conflicted rebase aborted so remaining children process"
unset LOOM_TEST_REBASE_EXIT

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing rebase-stacked-children.sh source guards..."
src="$(cat "$RSC_SRC")"
assert_contains "$src" 'gh pr list --repo "$REPO_NWO" --base "$parent_branch" --state open' \
  "script discovers children via a live forge query, not the daemon registry"
assert_contains "$src" 'git merge-base --is-ancestor "origin/$parent_branch" "origin/$child_branch"' \
  "script determines staleness via git merge-base --is-ancestor"
assert_contains "$src" "grep -qx 'loom:building'" \
  "script gates safe/unsafe on the child issue's loom:building label"
assert_contains "$src" 'run git rebase "origin/$parent_branch" "$child_branch"' \
  "safe path rebases the child onto the parent tip"
assert_contains "$src" "run git push --force-with-lease" \
  "safe path publishes with --force-with-lease (never bare --force)"
assert_not_contains "$src" "gh pr edit" \
  "script never retargets the child PR base (stays stacked on the parent)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
