#!/usr/bin/env bash
# test-merge-pr-merge-ordering-guard.sh - Unit tests for the PRE-merge
# merge-ordering guard in merge-pr.sh (#3747, stacked-PR v2 item 2; reshaped
# by #7982 into pin-and-warn).
#
# Before either the auto-merge or synchronous-merge path attempts the actual
# merge API call, merge-pr.sh now runs a guard that discovers open CHILD PRs
# still targeting the PARENT branch (feature/issue-<N>) via a LIVE forge query
# (`gh pr list --base <parent> --state open`, never the daemon registry).
#
# #7982 reshaped the guard's default behavior: instead of hard-blocking every
# time an open child exists, it now PINS the parent's pre-merge tip to
# refs/loom/parent/<branch> — a ref reconcile-stack.sh
# can fall back to once delete_branch_on_merge removes the branch itself — and
# proceeds with a loud WARNING naming each child PR and the exact
# reconcile-stack.sh invocation to run once the parent lands. The guard still
# HARD-BLOCKS (error, exit 1) only when the tip could not be pinned (a
# detached/unreadable parent) — that is the one case where the original #3747
# failure is genuinely still reachable. --allow-stacked-children skips
# straight past that remaining block. --dry-run never mutates local refs; it
# reports the would-be outcome without pinning or exiting 1. The guard is a
# no-op for non-feature/issue-N parent branches and non-GitHub forges, and
# keys purely on "does an open child PR target this branch" (NOT on the child
# issue's loom:building label — that split is item 1's concern).
#
# Strategy (mirrors test-merge-pr-auto-reconcile.sh, extended for #7982): the
# function under test (_check_no_open_stacked_children) depends only on globals
# (PR_BRANCH, PR_HEAD_SHA, REPO_ROOT, REPO_NWO, FORGE_TYPE, DRY_RUN,
# ALLOW_STACKED_CHILDREN) plus the `gh` CLI and REAL git (so the pin/verify
# plumbing is exercised for real, not mocked). We extract the function
# definition from merge-pr.sh and source it, stub `gh` on PATH to serve canned
# child-PR lists, point REPO_ROOT at a small real git sandbox, then assert on
# the guard's exit code + emitted message + the actual ref state.
# Because the block path calls `error` (which `exit 1`s), the guard is invoked
# inside a command-substitution subshell so the exit does not tear down the
# test. Extracting from source (rather than replicating) keeps the test in
# lockstep with the script.
#
# #8191 (slice): the decision itself is now `loom-daemon merge-pr
# stacked-children` (Rust), which merge-pr.sh calls out to. NOTHING about this
# suite's strategy changes — the extracted stub still reads the same globals,
# the daemon still queries the SAME stubbed `gh` on PATH and writes the pin into
# the SAME real git sandbox, so every behavioral assertion below runs unchanged
# against the port. What could not survive is the handful of assertions that
# grepped merge-pr.sh for the `jq`/`git` pipelines themselves; each is printed
# by `retired()` with the behavioral successor that pins the same property, per
# the three-part test in defaults/docs/verification-recipes.md §6. Two new cases
# (T11) pin the port's own new failure mode: a verb that cannot answer.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-merge-ordering-guard.sh

# SC2034: several globals (PR_BRANCH, PR_HEAD_SHA, REPO_ROOT, REPO_NWO,
# FORGE_TYPE, DRY_RUN, ALLOW_STACKED_CHILDREN) are read only by the function
# extracted+sourced from merge-pr.sh, which shellcheck cannot see — every such
# assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'  # retired() below
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

# An assertion that CANNOT survive the #8191 port of this guard to Rust,
# retired under the three-part test in defaults/docs/verification-recipes.md §6.
# Printed, not deleted: a reader must be able to see what was removed, why, and
# what proves the property now. Counted as run so the totals stay honest.
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
}

assert_contains() {
    local haystack="$1" needle="$2" msg="$3"
    TESTS_RUN=$((TESTS_RUN + 1))
    # Here-string, not a pipe, so grep -q exiting early on a match cannot
    # SIGPIPE printf and (under set -o pipefail) flip the pipeline non-zero
    # despite a match — a size-sensitive flake on large haystacks (#3820).
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
    if ! grep -qF -- "$needle" <<<"$haystack"; then
        TESTS_PASSED=$((TESTS_PASSED + 1))
        echo -e "  ${GREEN}PASS${NC}: $msg"
    else
        TESTS_FAILED=$((TESTS_FAILED + 1))
        echo -e "  ${RED}FAIL${NC}: $msg"
        echo "    Unexpected substring: '$needle'"
        echo "    In: '$haystack'"
    fi
}

# --- Minimal logging/error shims the extracted function calls ---
# `error` must exit non-zero to faithfully model the real script's hard block;
# the guard is always invoked in a subshell (see run_guard) so this exit only
# tears down that subshell, not the test.
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }
error()   { echo "ERROR: $*" >&2; exit 1; }

# --- Extract the function under test from merge-pr.sh and source it ---
# From `_check_no_open_stacked_children() {` up to (not including) the
# `# Invoke the guard before` invocation comment. The #7982 pin/warn/block
# decision is inline in that same function (merge-pr.sh is ratcheted and a new
# sibling shell lib is not an available remedy — see the comment above the
# function in merge-pr.sh), so this one extraction covers all of it.
# Extracting from source keeps the test in lockstep with the script.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
SANDBOX_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" "$SANDBOX_DIR" 2>/dev/null || true' EXIT

awk '
  /^_check_no_open_stacked_children\(\) \{/ { capture=1 }
  /^# Invoke the guard before/              { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_check_no_open_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _check_no_open_stacked_children from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub gh on PATH ---
#   gh pr list --base B ...  -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
# The --base value contains a '/' (feature/issue-N), so the stub sanitizes it to
# '_' before building the fixture filename; the test writes fixtures the same way.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
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
echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"

git_q() { git -c advice.detachedHead=false -c protocol.file.allow=always "$@"; }

# --- Real git sandbox for REPO_ROOT (the guard's pin path does real git I/O) ---
# A bare "origin" plus a working clone (REPO_ROOT) with a parent branch
# (feature/issue-100) pushed and its tip left checked out locally, so
# `git cat-file -e` finds the object without needing the fetch fallback.
# PARENT_SHA is the real, resolvable commit used as PR_HEAD_SHA in the
# pin-succeeds tests below.
ORIGIN_BARE="$SANDBOX_DIR/origin.git"
REPO_ROOT="$SANDBOX_DIR/repo-root"
git_q init --quiet --bare "$ORIGIN_BARE"
git_q init --quiet "$REPO_ROOT"
git_q -C "$REPO_ROOT" config user.email "test@loom.local"
git_q -C "$REPO_ROOT" config user.name "Loom Test"
git_q -C "$REPO_ROOT" config commit.gpgsign false
git_q -C "$REPO_ROOT" checkout -q -b main
echo "base" > "$REPO_ROOT/base.txt"
git_q -C "$REPO_ROOT" add base.txt
git_q -C "$REPO_ROOT" commit -q -m "base"
git_q -C "$REPO_ROOT" remote add origin "$ORIGIN_BARE"
git_q -C "$REPO_ROOT" push -q -u origin main

git_q -C "$REPO_ROOT" checkout -q -b feature/issue-100
echo "parent" > "$REPO_ROOT/parent.txt"
git_q -C "$REPO_ROOT" add parent.txt
git_q -C "$REPO_ROOT" commit -q -m "parent tip"
PARENT_SHA="$(git_q -C "$REPO_ROOT" rev-parse HEAD)"
git_q -C "$REPO_ROOT" push -q -u origin feature/issue-100
git_q -C "$REPO_ROOT" checkout -q main

# A SHA that exists nowhere (not in REPO_ROOT, not in origin) — used to force
# the pin to fail (T8): neither the fast local-object-store check nor the
# fetch fallback can ever make it resolve.
UNRESOLVABLE_SHA="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

# --- Shared globals the guard reads (see the file-level SC2034 disable). ---
PR_NUMBER="999"
REPO_NWO="owner/repo"
FORGE_TYPE="github"
DRY_RUN=false
ALLOW_STACKED_CHILDREN=false
PR_HEAD_SHA="$PARENT_SHA"

# Fixture writers (base -> sanitized filename).
write_prlist() { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist() { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }

# Clears any refs/loom/parent/<branch> ref left over from a previous test case.
clear_pin_ref() { git_q -C "$REPO_ROOT" update-ref -d "refs/loom/parent/$1" 2>/dev/null || true; }

# Run the guard in a subshell (its block path calls `error`, which exit 1's),
# capturing combined stdout+stderr in LAST_OUT and the exit code in LAST_RC.
LAST_OUT=""
LAST_RC=0
run_guard() {
    set +e
    LAST_OUT="$( _check_no_open_stacked_children 2>&1 )"
    LAST_RC=$?
    set -e
}

echo "Testing _check_no_open_stacked_children behavior..."

# T1: no open children -> guard passes (rc 0), no block, no pin attempted.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
clear_prlist "feature/issue-100"   # stub returns [] with no fixture
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "0" "$LAST_RC" "No open children -> guard passes (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "No open children -> no block message"
assert_eq "" "$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)" \
  "No open children -> no parent-pin ref written"

# T2: one open child targeting the parent branch, tip pinnable -> guard now
# PINS the tip and PROCEEDS (exit 0) with a warning naming the child PR and
# the reconcile-stack.sh unblock command, instead of hard-blocking (#7982).
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "0" "$LAST_RC" "Open child #501, tip pinnable -> guard proceeds (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Pin succeeds -> no hard block message"
assert_contains "$LAST_OUT" "Pinned the parent tip" "Pin succeeds -> warning names the pin"
assert_contains "$LAST_OUT" "refs/loom/parent/feature/issue-100" "Pin succeeds -> warning names the pinned ref"
assert_contains "$LAST_OUT" "#501" "Pin-succeeds warning names the child PR #501"
assert_contains "$LAST_OUT" "reconcile-stack.sh 501 feature/issue-100" \
  "Pin-succeeds warning gives the exact per-child reconcile-stack.sh invocation"
PINNED_SHA="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)"
assert_eq "$PARENT_SHA" "$PINNED_SHA" \
  "refs/loom/parent/feature/issue-100 actually pinned to the parent's tip SHA"

# T3: --allow-stacked-children with an open child present -> merge proceeds
# (rc 0); a warning is emitted but no hard block, AND no pin is attempted
# (the bypass short-circuits before the pin path runs).
DRY_RUN=false; ALLOW_STACKED_CHILDREN=true
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "0" "$LAST_RC" "--allow-stacked-children + open child -> guard proceeds (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "--allow-stacked-children -> no hard block"
assert_contains "$LAST_OUT" "--allow-stacked-children set" "--allow-stacked-children -> override warning emitted"
assert_eq "" "$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)" \
  "--allow-stacked-children -> bypass skips pinning entirely"
ALLOW_STACKED_CHILDREN=false

# T4: non-feature/issue-N parent branch -> guard skipped entirely (rc 0), even
# though the stub would return an open child for that base.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="release-1"
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "Non-feature/issue-N parent 'release-1' -> guard skipped (exit 0)"
assert_not_contains "$LAST_OUT" "Merge blocked" "Non-feature/issue-N parent -> no block"

# T5: --dry-run with an open child present -> reports the predicted outcome
# WITHOUT exiting 1 (dry-run contract preserved) and WITHOUT pinning anything
# (dry-run must have zero side effects).
DRY_RUN=true; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "0" "$LAST_RC" "--dry-run + open child -> guard does NOT exit 1 (dry-run contract)"
assert_contains "$LAST_OUT" "[dry-run]" "--dry-run -> reports the predicted outcome"
assert_contains "$LAST_OUT" "#501" "--dry-run report names the open child PR #501"
assert_eq "" "$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)" \
  "--dry-run -> no ref actually pinned (zero side effects)"
DRY_RUN=false

# T6: FORGE_TYPE != github -> no-op (GitHub-only for v2 item 2).
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
FORGE_TYPE="gitea"
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
run_guard
assert_eq "0" "$LAST_RC" "FORGE_TYPE=gitea -> guard skipped (GitHub-only)"
assert_not_contains "$LAST_OUT" "Merge blocked" "FORGE_TYPE=gitea -> no block"
FORGE_TYPE="github"

# T7: multiple open children, tip pinnable -> proceeds, naming EACH child and
# its own reconcile-stack.sh invocation.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
write_prlist "feature/issue-100" \
  '[{"number":501,"headRefName":"feature/issue-201"},{"number":502,"headRefName":"feature/issue-202"}]'
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "0" "$LAST_RC" "Multiple open children, tip pinnable -> guard proceeds (exit 0)"
assert_contains "$LAST_OUT" "#501" "Multi-child pin-succeeds warning names child #501"
assert_contains "$LAST_OUT" "#502" "Multi-child pin-succeeds warning names child #502"
assert_contains "$LAST_OUT" "reconcile-stack.sh 501 feature/issue-100" \
  "Multi-child warning gives child #501's own reconcile-stack.sh invocation"
assert_contains "$LAST_OUT" "reconcile-stack.sh 502 feature/issue-100" \
  "Multi-child warning gives child #502's own reconcile-stack.sh invocation"

# T8 (#7982): tip CANNOT be pinned (PR_HEAD_SHA resolves nowhere, not locally
# and not via either fetch fallback) -> the guard still HARD-BLOCKS, exactly
# the one case where the original #3747 race remains reachable.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$UNRESOLVABLE_SHA"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
run_guard
assert_eq "1" "$LAST_RC" "Unpinnable parent tip -> merge still hard-blocked (exit 1)"
assert_contains "$LAST_OUT" "Merge blocked" "Unpinnable tip -> block message emitted"
assert_contains "$LAST_OUT" "could not be pinned" "Unpinnable tip -> block message explains why"
assert_contains "$LAST_OUT" "#501" "Unpinnable-tip block message still names the blocking child PR #501"
assert_contains "$LAST_OUT" "reconcile-stack.sh" "Unpinnable-tip block message points at the reconcile-stack.sh unblock path"
assert_contains "$LAST_OUT" "--allow-stacked-children" "Unpinnable-tip block message mentions the --allow-stacked-children override"
assert_eq "" "$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)" \
  "Unpinnable tip -> no ref was written"
PR_HEAD_SHA="$PARENT_SHA"

# T9 (#8010 item 2): the guard captures STACKED_CHILDREN_JSON (consumed by
# the post-merge reconcile AND the merge-time re-pin, #8010 items 2 and 3)
# exactly when it found an open child — never when there were none. Called
# DIRECTLY (not via run_guard, which wraps the call in a `$( … )` command
# substitution — a subshell whose plain-assignment globals never propagate
# back to this script) so the global side effect is actually observable.
DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
unset STACKED_CHILDREN_JSON 2>/dev/null || true
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
_check_no_open_stacked_children >/dev/null 2>&1 || true
assert_contains "${STACKED_CHILDREN_JSON:-}" '"number":501' \
  "Guard captures STACKED_CHILDREN_JSON with an open child present (#8010 item 2)"

unset STACKED_CHILDREN_JSON 2>/dev/null || true
clear_prlist "feature/issue-100"
clear_pin_ref "feature/issue-100"
_check_no_open_stacked_children >/dev/null 2>&1 || true
assert_eq "" "${STACKED_CHILDREN_JSON:-}" \
  "Guard does NOT set STACKED_CHILDREN_JSON when there are no open children"

# T9b (Judge fix, #8010 follow-up): STACKED_CHILDREN_PIN_WRITTEN must track
# whether a pin was actually WRITTEN, not merely whether an open child was
# FOUND. The --allow-stacked-children bypass finds a child (sets
# STACKED_CHILDREN_JSON) but returns before ever reaching the pin write, so
# STACKED_CHILDREN_PIN_WRITTEN must stay unset in that path even though a real
# pin-succeeds run sets it.
unset STACKED_CHILDREN_PIN_WRITTEN STACKED_CHILDREN_JSON 2>/dev/null || true
DRY_RUN=false; ALLOW_STACKED_CHILDREN=true
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
clear_pin_ref "feature/issue-100"
_check_no_open_stacked_children >/dev/null 2>&1 || true
assert_eq "" "${STACKED_CHILDREN_PIN_WRITTEN:-}" \
  "--allow-stacked-children bypass finds a child but STACKED_CHILDREN_PIN_WRITTEN stays unset (no pin was written)"
ALLOW_STACKED_CHILDREN=false

unset STACKED_CHILDREN_PIN_WRITTEN 2>/dev/null || true
clear_pin_ref "feature/issue-100"
_check_no_open_stacked_children >/dev/null 2>&1 || true
assert_eq "true" "${STACKED_CHILDREN_PIN_WRITTEN:-}" \
  "A genuine pin-succeeds run sets STACKED_CHILDREN_PIN_WRITTEN=true"

# T10 (#8010 item 3): the merge-time re-pin. This is top-level script code
# (not a function — it runs once, right after $MERGE_PRECONDITION_SHA is
# refreshed), so it is extracted by unique CONTENT rather than by the
# name()-matching `awk` extraction the rest of this file uses, keeping the
# test in lockstep with the exact line shipped.
REPIN_LINE="$(grep -F 'MERGE_PRECONDITION_SHA" != "$PR_HEAD_SHA"' "$MERGE_PR_SRC")"
if [[ -z "$REPIN_LINE" ]]; then
    echo -e "${RED}FATAL${NC}: could not find the #8010 item 3 re-pin line in $MERGE_PR_SRC" >&2
    exit 2
fi

# A second commit on the parent branch simulates it being pushed again
# between the pre-merge guard's pin (item 2's PARENT_SHA) and the live
# merge-time SHA read — the exact race item 3 closes.
git_q -C "$REPO_ROOT" checkout -q feature/issue-100
echo "parent v2" > "$REPO_ROOT/parent.txt"
git_q -C "$REPO_ROOT" add parent.txt
git_q -C "$REPO_ROOT" commit -q -m "parent tip v2"
PARENT_SHA_V2="$(git_q -C "$REPO_ROOT" rev-parse HEAD)"
git_q -C "$REPO_ROOT" push -q origin feature/issue-100
git_q -C "$REPO_ROOT" checkout -q main

# (a) SHA drifted + a pin was actually WRITTEN -> the pin moves to the fresh SHA.
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
STACKED_CHILDREN_PIN_WRITTEN=true
clear_pin_ref "feature/issue-100"
git_q -C "$REPO_ROOT" update-ref "refs/loom/parent/feature/issue-100" "$PARENT_SHA"
MERGE_PRECONDITION_SHA="$PARENT_SHA_V2"
eval "$REPIN_LINE" || true
REPINNED_SHA="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100)"
assert_eq "$PARENT_SHA_V2" "$REPINNED_SHA" \
  "Re-pin moves refs/loom/parent/<branch> to the freshly-read merge-time SHA when it drifted (#8010 item 3)"

# (b) No drift -> the pin is left exactly as the guard wrote it.
clear_pin_ref "feature/issue-100"
git_q -C "$REPO_ROOT" update-ref "refs/loom/parent/feature/issue-100" "$PARENT_SHA"
MERGE_PRECONDITION_SHA="$PARENT_SHA"
eval "$REPIN_LINE" || true
UNCHANGED_SHA="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100)"
assert_eq "$PARENT_SHA" "$UNCHANGED_SHA" \
  "Re-pin is a no-op when the merge-time SHA matches what the guard already pinned"

# (c) No pin was ever WRITTEN (STACKED_CHILDREN_PIN_WRITTEN unset) -> nothing
# is written even though the SHAs differ (no orphan pin for an unrelated PR).
clear_pin_ref "feature/issue-100"
unset STACKED_CHILDREN_PIN_WRITTEN 2>/dev/null || true
MERGE_PRECONDITION_SHA="$PARENT_SHA_V2"
PR_HEAD_SHA="$PARENT_SHA"
eval "$REPIN_LINE" || true
NO_PIN="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)"
assert_eq "" "$NO_PIN" \
  "Re-pin writes nothing when no pre-merge pin was written (STACKED_CHILDREN_PIN_WRITTEN unset)"

# (e) Judge fix regression (#8010 follow-up): --allow-stacked-children bypass
# + drift. The guard's bypass path finds an open child (sets
# STACKED_CHILDREN_JSON) but returns without ever writing a pin, so
# STACKED_CHILDREN_JSON alone is NOT proof a pin exists. Before the fix, the
# re-pin line gated on STACKED_CHILDREN_JSON and would still fire here,
# silently creating a pin the guard itself declined to create.
clear_pin_ref "feature/issue-100"
unset STACKED_CHILDREN_PIN_WRITTEN STACKED_CHILDREN_JSON 2>/dev/null || true
STACKED_CHILDREN_JSON='[{"number":501,"headRefName":"feature/issue-201"}]'
MERGE_PRECONDITION_SHA="$PARENT_SHA_V2"
PR_HEAD_SHA="$PARENT_SHA"
eval "$REPIN_LINE" || true
BYPASS_DRIFT_PIN="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)"
assert_eq "" "$BYPASS_DRIFT_PIN" \
  "--allow-stacked-children bypass + SHA drift -> re-pin still writes nothing (STACKED_CHILDREN_JSON alone is not proof a pin exists)"
unset STACKED_CHILDREN_JSON 2>/dev/null || true

# (f) --dry-run must never mutate the pin, even with a genuine SHA drift and a
# real pin write — this line runs unconditionally ahead of both merge paths'
# own dry-run checks, so it needs its own guard.
clear_pin_ref "feature/issue-100"
DRY_RUN=true
STACKED_CHILDREN_PIN_WRITTEN=true
PR_HEAD_SHA="$PARENT_SHA"
MERGE_PRECONDITION_SHA="$PARENT_SHA_V2"
eval "$REPIN_LINE" || true
DRY_RUN_PIN="$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)"
assert_eq "" "$DRY_RUN_PIN" \
  "--dry-run -> re-pin writes nothing even with a genuine SHA drift (zero side effects)"
DRY_RUN=false

clear_pin_ref "feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
unset STACKED_CHILDREN_PIN_WRITTEN 2>/dev/null || true

# --- T11 (#8191): a verb that cannot ANSWER is a guard fault, not a refusal ---
#
# The port gives this path a failure mode the shell implementation could not
# have: the subcommand does not answer. Two things must hold, and they pull in
# opposite directions, which is why both are pinned.
#
#   1. It must NOT be mistaken for "merge refused". A stacked-children guard
#      that hard-blocks whenever its binary is a release behind would stop every
#      merge on that host — including the overwhelming majority of PRs that have
#      no stacked children at all. Unlike the review gates further down this
#      script, this guard establishes a postcondition for a best-effort
#      POST-merge cleanup step, so it fails OPEN, loudly.
#   2. It must not be mistaken for "merge succeeded either" — i.e. it must not
#      be SILENT. The warning has to carry the recovery material (the tip that
#      would have been pinned), because that SHA is what a manual
#      reconcile-stack.sh needs once the forge has deleted the branch.
#
# An open child is present in all three cases, so the guard is genuinely live:
# a fault that only showed up on the no-children path would prove nothing.
echo ""
echo "Test 11: a stacked-children verb that cannot answer warns and proceeds (fail-open), never blocks"

DRY_RUN=false; ALLOW_STACKED_CHILDREN=false
PR_BRANCH="feature/issue-100"
PR_HEAD_SHA="$PARENT_SHA"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'

# (a) the binary does not exist at all — an un-rolled host, the #8285 case.
clear_pin_ref "feature/issue-100"
set +e
OUT_A="$(LOOM_DAEMON_BIN="$SANDBOX_DIR/no-such-loom-daemon" _check_no_open_stacked_children 2>&1)"
RC_A=$?
set -e
assert_eq "0" "$RC_A" "(a) an unresolvable loom-daemon does not block the merge (exit 0)"
assert_not_contains "$OUT_A" "Merge blocked" "(a) an unresolvable binary is not read as a refusal"
assert_contains "$OUT_A" "did not run" "(a) the fault is reported, not swallowed"
assert_contains "$OUT_A" "$PARENT_SHA" \
  "(a) the fault warning carries the tip that would have been pinned (the manual-reconcile recovery material)"
assert_eq "" "$(git_q -C "$REPO_ROOT" rev-parse --verify --quiet refs/loom/parent/feature/issue-100 2>/dev/null || true)" \
  "(a) nothing ran, so no ref was written"

# (b) a binary that EXITS 1 but prints no BLOCK record. Exit 1 is this verb's
# refusal code, so this is the case that decides whether the stub trusts the
# exit code alone or requires the record: clap uses other codes for an unknown
# subcommand today, but a wrapper, a shim or a future clap could land on 1.
cat >"$SANDBOX_DIR/silent-one" <<'SILENT'
#!/usr/bin/env bash
exit 1
SILENT
chmod +x "$SANDBOX_DIR/silent-one"
clear_pin_ref "feature/issue-100"
set +e
OUT_B="$(LOOM_DAEMON_BIN="$SANDBOX_DIR/silent-one" _check_no_open_stacked_children 2>&1)"
RC_B=$?
set -e
assert_eq "0" "$RC_B" "(b) exit 1 with no BLOCK record is a fault, not a refusal (exit 0)"
assert_contains "$OUT_B" "did not run" "(b) the bare exit 1 is reported as a fault"
assert_not_contains "$OUT_B" "Merge blocked" "(b) a refusal is never synthesized from an exit code alone"

# (c) the converse: exit 1 WITH a BLOCK record IS a refusal, and the record's
# own text is what the operator sees. Together with (b) this pins the contract
# as "code AND record", not either alone.
cat >"$SANDBOX_DIR/blocking" <<'BLOCKING'
#!/usr/bin/env bash
printf 'CHILDREN\t[{"number":501,"headRefName":"feature/issue-201"}]\n'
printf 'BLOCK\tMerge blocked: synthetic refusal from the stub daemon\n'
exit 1
BLOCKING
chmod +x "$SANDBOX_DIR/blocking"
clear_pin_ref "feature/issue-100"
set +e
OUT_C="$(LOOM_DAEMON_BIN="$SANDBOX_DIR/blocking" _check_no_open_stacked_children 2>&1)"
RC_C=$?
set -e
assert_eq "1" "$RC_C" "(c) exit 1 WITH a BLOCK record refuses the merge (exit 1)"
assert_contains "$OUT_C" "synthetic refusal from the stub daemon" \
  "(c) the refusal the operator sees is the daemon's own BLOCK text, replayed through error()"

clear_pin_ref "feature/issue-100"
unset STACKED_CHILDREN_JSON STACKED_CHILDREN_PIN_WRITTEN 2>/dev/null || true

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_check_no_open_stacked_children" \
  "merge-pr.sh defines and invokes _check_no_open_stacked_children"
# The guard now calls out to `loom-daemon merge-pr stacked-children`, so the
# five greps below no longer have a pipeline in THIS file to find. Each is
# retired with the behavioral successor that pins the same property — every one
# of which is an assertion already in this suite, running against the port.
assert_contains "$src" "merge-pr stacked-children" \
  "merge-pr.sh routes the merge-ordering guard through loom-daemon merge-pr stacked-children (#8191)"
retired "merge-pr.sh's source writes the PR head SHA to the pin ref (update-ref \"\$pin\" \"\$PR_HEAD_SHA\")" \
    "the guard must ESTABLISH the postcondition reconcile-stack.sh needs, not merely report it — a guard that warns without writing the ref leaves the #3747 race fully live" \
    "the update-ref call is no longer in this file; it is establish_pin() in loom-daemon/src/merge_pr/stacked_children.rs, so no grep of merge-pr.sh can pass" \
    "T2's own 'refs/loom/parent/feature/issue-100 actually pinned to the parent's tip SHA' assertion reads the REF back out of the real git sandbox after the port ran — it fails if nothing was written, which a source grep cannot detect. merge_pr::stacked_children::tests::a_resolvable_tip_is_pinned_to_the_ref_reconcile_stack_reads pins the same property directly."
retired "merge-pr.sh's source contains pin=\"refs/loom/parent/\$PR_BRANCH\"" \
    "the pin must live in the namespace reconcile-stack.sh's fallback reads; a pin written anywhere else is invisible to the tool that needs it" \
    "the pin path is now pin_ref() in Rust; this file only names the namespace in the #8010 item-3 re-pin line, which is not this function" \
    "T2 asserts the ref at the exact path 'refs/loom/parent/feature/issue-100' holds the parent SHA, and T2's message assertions require the warning to NAME that path. merge_pr::stacked_children::tests::the_pin_ref_lives_under_the_namespace_reconcile_stack_reads pins the construction, and test-reconcile-stack.sh independently pins the consumer side of the same namespace."
retired "merge-pr.sh's source verifies the commit object exists (cat-file -e \"\${PR_HEAD_SHA}^{commit}\")" \
    "a ref pointing at an object this repo does not have satisfies 'did we write a ref' while being useless to a later rebase — so the object must be verified, and the ^{commit} peel is what rejects a readable non-commit" \
    "the verify/fetch/re-verify chain is now establish_pin() in Rust; the grep cannot pass against this file" \
    "T8 is the behavioral successor and is strictly stronger: an unresolvable SHA must both hard-block AND leave NO ref behind, which is exactly the failure a missing object check produces. merge_pr::stacked_children::tests::a_non_commit_object_does_not_satisfy_the_pin pins the ^{commit} peel itself — a property the retired grep did NOT pin, since a naive port could have kept the literal text and dropped the peel."
retired "merge-pr.sh's source builds the per-child command via jq (reconcile-stack.sh \" + (.number|tostring))" \
    "each child must get its own paste-ready unblock command; a drift in the indentation, the script path or the argument order hands the operator a command that does not run" \
    "the command list is now reconcile_commands() in Rust; this file renders no jq" \
    "T2 and T7 assert the exact strings 'reconcile-stack.sh 501 feature/issue-100' and '… 502 …' appear in the guard's own output, and T8 asserts the block message points at reconcile-stack.sh too. loom-daemon/tests/merge_pr_stacked_children_differential.rs additionally proves the port's rendering is byte-for-byte identical to the RETIRED jq on a 21-entry corpus fed to both sides from one file — a scan checks the jq text is present; the differential checks the output is identical, which subsumes it."
retired "merge-pr.sh's source contains the gh pr list --base <parent> --state open discovery call" \
    "children must be discovered by a LIVE forge query, never the ephemeral daemon SweepRegistry — terminal registry entries are GC'd ~1h after transition and the registry only exists while loom-daemon runs, but this guard also runs from Champion's cron" \
    "the query is now discover_open_children() in Rust; merge-pr.sh issues no gh call for it (the surviving 'gh pr list --base' in this file belongs to the POST-merge reconcile, a different function)" \
    "every behavioral case in this suite (T1-T9b) is served by the PATH-stubbed \`gh\` that only answers \`pr list --base\`: if the port stopped querying gh, the stub would never be hit and T2/T7/T8 would all fail on an empty child list. merge_pr::stacked_children's discover_open_children owns the argv, and an_unrunnable_gh_discovers_no_children_rather_than_erroring pins the fail-open read."
assert_contains "$src" "ALLOW_STACKED_CHILDREN" \
  "merge-pr.sh threads the --allow-stacked-children override into the guard"
assert_contains "$src" "--allow-stacked-children) ALLOW_STACKED_CHILDREN=true" \
  "merge-pr.sh parses the --allow-stacked-children flag alongside the other options"
assert_contains "$src" "STACKED_CHILDREN_JSON=\"\$children_json\"" \
  "merge-pr.sh captures the pre-merge children snapshot into STACKED_CHILDREN_JSON (#8010 item 2)"
assert_contains "$src" 'update-ref "refs/loom/parent/$PR_BRANCH" "$MERGE_PRECONDITION_SHA"' \
  "merge-pr.sh re-pins to the freshly-read merge-time SHA when it drifted from the guard's pin (#8010 item 3)"
assert_contains "$src" "STACKED_CHILDREN_PIN_WRITTEN=true" \
  "merge-pr.sh sets STACKED_CHILDREN_PIN_WRITTEN only at the point the pin write actually succeeds"
assert_contains "$src" '"${STACKED_CHILDREN_PIN_WRITTEN:-}" == "true"' \
  "merge-pr.sh gates the item-3 re-pin on STACKED_CHILDREN_PIN_WRITTEN, not on STACKED_CHILDREN_JSON alone"

# Assert the guard is invoked BEFORE the auto-merge path (line ordering): the
# _check_no_open_stacked_children invocation must precede `# Handle auto-merge mode`.
guard_line="$(grep -n '^_check_no_open_stacked_children$' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
automerge_line="$(grep -n '^# Handle auto-merge mode' "$MERGE_PR_SRC" | head -1 | cut -d: -f1)"
if [[ -n "$guard_line" && -n "$automerge_line" && "$guard_line" -lt "$automerge_line" ]]; then
    ordered="yes"
else
    ordered="no (guard=$guard_line automerge=$automerge_line)"
fi
assert_eq "yes" "$ordered" \
  "guard is invoked before both merge paths (before '# Handle auto-merge mode')"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
