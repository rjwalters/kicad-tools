#!/usr/bin/env bash
# test-merge-pr-auto-reconcile.sh - Unit tests for the automated stacked-PR
# reconciliation trigger in merge-pr.sh (#3747, stacked-PR v2 item 1).
#
# When a stacked PARENT PR (branch feature/issue-<N>) squash-merges, merge-pr.sh
# now discovers open CHILD PRs based on the parent branch via a LIVE forge query
# (`gh pr list --base <parent>`, never the daemon registry) and, per child:
#   - Safe   (child issue NOT loom:building): invokes reconcile-stack.sh.
#   - Unsafe (child issue still loom:building): skips the rebase and posts a
#     deferred-reconciliation comment on the child PR instead.
# The whole step is best-effort and must never fail the parent merge, and it is
# a no-op for non-feature/issue-N parent branches and non-GitHub forges.
#
# Strategy (mirrors test-merge-pr-partial-increment.sh): the functions under
# test (_auto_reconcile_stacked_children and _reconcile_one_stacked_child) depend
# only on globals (PR_BRANCH, REPO_NWO, FORGE_TYPE, SCRIPT_DIR), the `gh` CLI, and
# an invocation of $SCRIPT_DIR/reconcile-stack.sh. We extract the function
# definitions from merge-pr.sh and source them, stub `gh` on PATH to serve canned
# issue JSON / child-PR lists and record mutating calls, point SCRIPT_DIR at a
# stub reconcile-stack.sh that records its args, then assert on the recorded
# calls. Extracting from source (rather than replicating) keeps the test in
# lockstep with the script.
#
# Usage:
#   ./.loom/scripts/tests/test-merge-pr-auto-reconcile.sh

# SC2034: several globals (PR_BRANCH, REPO_NWO, FORGE_TYPE, SCRIPT_DIR) are read
# only by the functions extracted+sourced from merge-pr.sh, which shellcheck
# cannot see — every such assignment looks "unused" to the linter.
# shellcheck disable=SC2034

set -euo pipefail

TEST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$TEST_DIR/.." && pwd)"
MERGE_PR_SRC="$HELPERS_DIR/merge-pr.sh"

# #8191 slice: both decisions this suite is ABOUT now delegate to
# `loom-daemon merge-pr reconcile-plan` / `merge-pr reconcile-child`. Pin the
# binary and verify it HAS the leaf verbs — a binary carrying only the `merge-pr`
# group predates this slice, and this seam fails OPEN, so every case below would
# then take the "skip auto-reconciliation" path and report green having measured
# nothing. That is exactly what the pin exists to prevent: T1-T9 are the evidence
# the port preserved the retired shell's behaviour, so if they cannot run against
# the port, this suite FAILS rather than skips.
# shellcheck source=lib/require-daemon-bin.sh
source "$TEST_DIR/lib/require-daemon-bin.sh"
loom_test_require_daemon_bin "$HELPERS_DIR" "merge-pr reconcile-plan" \
    "merge-pr reconcile-child"
# shellcheck source=lib/write-scope-fixture.sh
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/write-scope-fixture.sh"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'   # retired() below
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

# An assertion that CANNOT survive the port to Rust, retired under the
# three-part test in defaults/docs/verification-recipes.md §6. Printed, not
# deleted: a reader must be able to see what was removed, why it could not
# survive, and what proves the property now. Counted as run so the totals stay
# honest.
retired() { # <what> <property> <why-structural> <successor>
    TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${YELLOW}RETIRED${NC}: $1"
    echo "      property:   $2"
    echo "      structural: $3"
    echo "      successor:  $4"
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

# --- Minimal logging shims the extracted functions call ---
info()    { echo "INFO: $*"; }
success() { echo "OK: $*"; }
warning() { echo "WARN: $*" >&2; }

# --- Real forge-helpers.sh (for the #4856 rate-limit-safe mutation wrappers) ---
# The deferred-reconciliation comment is no longer posted with a bare
# `gh pr comment` — it routes through forge_gh_comment_rl_safe (lib/forge-helpers.sh),
# which posts via `gh issue comment` and falls back to the REST comments endpoint
# (shared by issues and PRs) when the GraphQL mutation is rate-limited (#4856).
# Source the REAL helper (rather than shimming it) so this suite keeps exercising
# the actual `gh` invocation shape the wrapper produces, which the stub below
# records verbatim. Sourced BEFORE the shared globals are assigned:
# forge-helpers.sh initializes FORGE_TYPE="" at load time, which would otherwise
# clobber the FORGE_TYPE="github" the tests rely on.
# shellcheck source=../lib/forge-helpers.sh
source "$HELPERS_DIR/lib/forge-helpers.sh"

# --- Extract the functions under test from merge-pr.sh and source them ---
# From `_reset_one_partial_issue() {` up to (not including) the
# `# Handle auto-merge mode` line — this span contains the partial-increment
# functions AND the stacked-reconcile functions under test (plus intervening
# comments, harmless when sourced). Extracting from source keeps the test in
# lockstep with the script.
FUNCS_FILE="$(mktemp)"
STUB_DIR="$(mktemp -d)"
RECON_DIR="$(mktemp -d)"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" "$RECON_DIR" 2>/dev/null || true' EXIT
awk '
  /^_reset_one_partial_issue\(\) \{/ { capture=1 }
  /^# Handle auto-merge mode/        { capture=0 }
  capture { print }
' "$MERGE_PR_SRC" > "$FUNCS_FILE"

if ! grep -q '_auto_reconcile_stacked_children()' "$FUNCS_FILE"; then
    echo -e "${RED}FATAL${NC}: could not extract _auto_reconcile_stacked_children from $MERGE_PR_SRC" >&2
    exit 2
fi
# shellcheck disable=SC1090
source "$FUNCS_FILE"

# --- Stub reconcile-stack.sh (records its argv) ---
# SCRIPT_DIR (read by the sourced functions) points here so the safe-case
# invocation `$SCRIPT_DIR/reconcile-stack.sh <child-pr> <parent-branch>` hits the
# stub. LOOM_TEST_RECON_EXIT lets a test force a non-zero exit (conflict path).
SCRIPT_DIR="$RECON_DIR"
cat > "$RECON_DIR/reconcile-stack.sh" <<'RECON'
#!/usr/bin/env bash
LOG="${LOOM_TEST_RECON_LOG:?stub reconcile-stack: LOOM_TEST_RECON_LOG not set}"
echo "reconcile-stack.sh $*" >> "$LOG"
exit "${LOOM_TEST_RECON_EXIT:-0}"
RECON
chmod +x "$RECON_DIR/reconcile-stack.sh"
export LOOM_TEST_RECON_LOG="$RECON_DIR/recon-calls.log"

# --- Stub gh on PATH ---
#   gh api repos/OWNER/REPO/issues/N   -> cat $STUB_DIR/issue-N.json (or {})
#   gh pr list --base B ...            -> cat $STUB_DIR/prlist-<sanitized B>.json (or [])
#   gh issue comment|edit|reopen N ... -> record to $STUB_DIR/gh-calls.log
#   gh pr comment N ...                -> record to $STUB_DIR/gh-calls.log
# The deferred-reconciliation comment reaches the stub as `gh issue comment <pr>`
# (not `gh pr comment <pr>`) since #4856 routed it through
# forge_gh_comment_rl_safe — the REST comments endpoint is shared by issues and
# PRs, so the wrapper uses the issue-side CLI verb for both. The legacy
# `gh pr comment` arm is retained so an accidental regression back to the raw
# call still lands in the log (where the assertions, which now expect the
# `issue comment` shape, will flag it) instead of hitting the unhandled-args
# fallback with a confusing exit 3.
# The --base value contains a '/' (feature/issue-N), so the stub sanitizes it to
# '_' before building the fixture filename; the test writes fixtures the same way.
cat > "$STUB_DIR/gh" <<'STUB'
#!/usr/bin/env bash
STUB_DIR_FROM_ENV="${LOOM_TEST_STUB_DIR:?stub gh: LOOM_TEST_STUB_DIR not set}"
LOG="$STUB_DIR_FROM_ENV/gh-calls.log"

if [[ "$1" == "api" ]]; then
  # Find the api path: it is the args/repos… token, NOT necessarily the last
  # argument (the #9774 POST shape is `… --input -`, which ends in `-`).
  path=""
  for _a in "$@"; do
    case "$_a" in repos/*) path="$_a"; break ;; esac
  done
  # #9774: a POST to the comments endpoint is the daemon chokepoint's shape
  # (forge comment -> gh api --input -); this suite's subject is the
  # reconcile flow over the recorded gh ladder, so fail that POST and let
  # forge_gh_comment_rl_safe fall back to the recorded `issue comment` shape.
  # (Reads — the issue-N.json fetches — end at the issue number, not
  # /comments, and are unaffected.)
  case "$path" in
    */comments) exit 1 ;;
  esac
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
  # Recorded to a SEPARATE log from $LOG (which the comment assertions read),
  # so T10/T11 can assert the discovery query was never made without perturbing
  # any existing assertion.
  echo "pr list --base $base" >> "$STUB_DIR_FROM_ENV/discovery.log"
  canned="$STUB_DIR_FROM_ENV/prlist-$safe.json"
  if [[ -f "$canned" ]]; then cat "$canned"; else echo '[]'; fi
  exit 0
fi

if [[ "$1" == "pr" && "$2" == "comment" ]]; then
  echo "$*" >> "$LOG"
  exit 0
fi

# `gh issue comment|edit|reopen N ...` — the shapes the #4856 rate-limit-safe
# wrappers in lib/forge-helpers.sh emit on their happy path.
if [[ "$1" == "issue" ]]; then
  echo "$*" >> "$LOG"
  exit 0
fi

echo "stub gh: unhandled args: $*" >&2
exit 3
STUB
chmod +x "$STUB_DIR/gh"
export LOOM_TEST_STUB_DIR="$STUB_DIR"
export PATH="$STUB_DIR:$PATH"
# #9548: merge-pr.sh vets its write target through the write scope before it
# writes. It runs from a checkout registered as owner/repo (origin, .loom/, push
# reported to the permission probe), so the real decision admits it.
write_scope_register "$STUB_DIR/checkout" owner/repo
cd "$STUB_DIR/checkout"

# --- Shared globals the functions read (see the file-level SC2034 disable). ---
REPO_NWO="owner/repo"
FORGE_TYPE="github"

# Canned issue fixtures (child issue label state).
cat > "$STUB_DIR/issue-201.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:issue"}]}
EOF
cat > "$STUB_DIR/issue-202.json" <<'EOF'
{"state":"open","labels":[{"name":"loom:building"}]}
EOF

# Fixture writers (base -> sanitized filename).
write_prlist() { printf '%s\n' "$2" > "$STUB_DIR/prlist-${1//\//_}.json"; }
clear_prlist() { rm -f "$STUB_DIR/prlist-${1//\//_}.json"; }

reset_logs() {
    : > "$STUB_DIR/gh-calls.log"
    : > "$STUB_DIR/discovery.log"
    : > "$RECON_DIR/recon-calls.log"
    unset LOOM_TEST_RECON_EXIT
}
read_gh_log() { cat "$STUB_DIR/gh-calls.log" 2>/dev/null || true; }
read_recon()  { cat "$RECON_DIR/recon-calls.log" 2>/dev/null || true; }
read_discovery() { cat "$STUB_DIR/discovery.log" 2>/dev/null || true; }

# --- Fake daemons for the fail-OPEN cases (T10-T13) ---
#
# `loom_test_require_daemon_bin` pinned LOOM_DAEMON_SELF_BIN to a snapshot of the
# real binary, which is what T1-T9 measure against. These let one test swap in a
# binary that CANNOT answer, which is the disposition the seam has to survive:
# an unanswered verb must skip, never guess, because `reconcile` force-pushes.
FAKE_DIR="$(mktemp -d)"
REAL_DAEMON_BIN="$LOOM_DAEMON_SELF_BIN"
trap 'rm -rf "$FUNCS_FILE" "$STUB_DIR" "$RECON_DIR" "$FAKE_DIR" 2>/dev/null || true' EXIT

# A binary predating this slice: clap rejects the unknown leaf verb with exit 2
# and a usage message on stderr, printing NOTHING on stdout.
cat > "$FAKE_DIR/daemon-no-verbs" <<'FAKE'
#!/usr/bin/env bash
echo "error: unrecognized subcommand '${3:-}'" >&2
exit 2
FAKE

# A binary that answers `reconcile-plan` for real but cannot answer
# `reconcile-child` — the partial-availability case that isolates the child
# verdict, which is the one that decides whether a branch gets force-pushed.
cat > "$FAKE_DIR/daemon-no-child" <<'FAKE'
#!/usr/bin/env bash
if [[ "${2:-}" == "reconcile-child" ]]; then
  echo "error: unrecognized subcommand 'reconcile-child'" >&2
  exit 2
fi
exec "$LOOM_TEST_REAL_DAEMON" "$@"
FAKE

# A binary that exits 0 for `reconcile-child` but prints something that is not a
# verdict. Exit status alone must not be read as consent.
cat > "$FAKE_DIR/daemon-child-garbage" <<'FAKE'
#!/usr/bin/env bash
if [[ "${2:-}" == "reconcile-child" ]]; then
  echo "warning: token pool refreshed"
  exit 0
fi
exec "$LOOM_TEST_REAL_DAEMON" "$@"
FAKE

chmod +x "$FAKE_DIR"/daemon-*
export LOOM_TEST_REAL_DAEMON="$REAL_DAEMON_BIN"

# Run one call with the daemon pinned to a fake, then restore. Explicit
# save/restore rather than an assignment-prefixed call: whether those persist
# past a FUNCTION invocation differs between bash's posix and default modes, and
# a leaked pin would silently un-pin every test after it.
with_daemon() {
    local fake="$1" saved_self="${LOOM_DAEMON_SELF_BIN:-}" saved_bin="${LOOM_DAEMON_BIN:-}" rc=0
    export LOOM_DAEMON_SELF_BIN="$fake" LOOM_DAEMON_BIN="$fake"
    _auto_reconcile_stacked_children || rc=$?
    export LOOM_DAEMON_SELF_BIN="$saved_self" LOOM_DAEMON_BIN="$saved_bin"
    return "$rc"
}

echo "Testing _auto_reconcile_stacked_children behavior..."

# T1: no open children -> no-op (no reconcile, no comment).
reset_logs
PR_BRANCH="feature/issue-100"
clear_prlist "feature/issue-100"   # stub returns [] with no fixture
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "No open children -> reconcile-stack.sh NOT invoked"
assert_eq "" "$(read_gh_log)" "No open children -> no comment posted"

# T2: safe child (issue not loom:building) -> reconcile-stack.sh invoked with the
# child PR number and the parent branch; no comment posted.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Safe child #501 (issue #201 not building) -> reconcile-stack.sh 501 feature/issue-100"
assert_eq "" "$(read_gh_log)" "Safe child -> no deferred-reconciliation comment"

# T3: unsafe child (issue still loom:building) -> comment posted on the child PR;
# reconcile-stack.sh NOT invoked.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":502,"headRefName":"feature/issue-202"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "Unsafe child #502 (issue #202 building) -> reconcile-stack.sh NOT invoked"
# Since #4856 the comment is posted through forge_gh_comment_rl_safe, whose
# happy path is `gh issue comment <pr> --repo <nwo> --body ...` (the REST
# comments endpoint it falls back to is shared by issues and PRs, so the wrapper
# uses one CLI verb for both) — assert on that shape, not the pre-#4856
# `gh pr comment` literal.
assert_contains "$(read_gh_log)" "issue comment 502 --repo owner/repo" \
  "Unsafe child -> deferred-reconciliation comment posted on PR #502"

# T4: non-feature/issue-N parent branch -> step skipped entirely (no discovery).
reset_logs
PR_BRANCH="release-1"
write_prlist "release-1" '[{"number":503,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "Non-feature/issue-N parent 'release-1' -> reconcile skipped"
assert_eq "" "$(read_gh_log)" "Non-feature/issue-N parent -> no comment, no discovery"

# T5: FORGE_TYPE != github -> no-op (GitHub-only for v2 item 1).
reset_logs
FORGE_TYPE="gitea"
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "FORGE_TYPE=gitea -> reconcile skipped (GitHub-only)"
assert_eq "" "$(read_gh_log)" "FORGE_TYPE=gitea -> no comment"
FORGE_TYPE="github"

# T6: reconcile-stack.sh failure (rebase conflict) is swallowed — the function
# still returns 0 (best-effort, never fails the parent merge).
reset_logs
export LOOM_TEST_RECON_EXIT=2
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
rc=0
_auto_reconcile_stacked_children || rc=$?
assert_eq "0" "$rc" "reconcile-stack.sh failure -> function still returns 0 (best-effort)"
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Failing reconcile still attempted the safe child"
unset LOOM_TEST_RECON_EXIT

# T7: multiple children, mixed safe/unsafe -> each handled independently.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" \
  '[{"number":501,"headRefName":"feature/issue-201"},{"number":502,"headRefName":"feature/issue-202"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Mixed set: safe child #501 reconciled"
assert_not_contains "$(read_recon)" "502" \
  "Mixed set: unsafe child #502 NOT reconciled"
assert_contains "$(read_gh_log)" "issue comment 502 --repo owner/repo" \
  "Mixed set: unsafe child #502 got a deferred comment"

# T8 (#8010 item 2): when STACKED_CHILDREN_JSON is already populated (the
# pre-merge guard's snapshot), the post-merge query must NOT be re-run — the
# function has to reconcile the child the pre-merge snapshot names even
# though a fresh `gh pr list` would return zero rows (simulating GitHub
# having already retargeted the child once delete_branch_on_merge removed the
# parent branch).
reset_logs
PR_BRANCH="feature/issue-100"
clear_prlist "feature/issue-100"   # a post-merge re-query would see []
STACKED_CHILDREN_JSON='[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "Pre-merge snapshot (STACKED_CHILDREN_JSON) drives reconciliation even though a post-merge re-query would see zero rows"
unset STACKED_CHILDREN_JSON

# T9 (#8010 item 2): with no pre-merge snapshot at all (STACKED_CHILDREN_JSON
# unset — e.g. the guard never ran), behavior falls back to the live
# post-merge query unchanged.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
unset STACKED_CHILDREN_JSON 2>/dev/null || true
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "No pre-merge snapshot -> falls back to the live post-merge query (unchanged behavior)"

# --- T10-T13 (#8191 slice): the seam fails OPEN, and silence is never a route ---
#
# Both decisions now come from `loom-daemon merge-pr reconcile-plan` /
# `reconcile-child`. T1-T9 above prove the port agrees with the retired shell
# when the daemon answers. These prove what happens when it CANNOT — the case
# that did not exist before this slice and is the only one that can turn a
# missing binary into a `git rebase --onto` + `push --force-with-lease` over a
# branch a Builder still has checked out.
#
# The rule being pinned: `reconcile` is reachable ONLY through a positive
# LOOM-RECONCILE-CHILD verdict. No output, a non-zero exit, and a zero exit with
# unrecognised output must all skip.

# T10: a daemon predating the slice cannot answer the PLAN. Skip the whole pass
# — and, because an unanswered gate is indistinguishable from NOT-STACKED, do it
# BEFORE spending a `gh pr list` on a rollup nothing will read.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
unset STACKED_CHILDREN_JSON 2>/dev/null || true
rc=0
with_daemon "$FAKE_DIR/daemon-no-verbs" 2>/dev/null || rc=$?
assert_eq "0" "$rc" "Unanswerable reconcile-plan -> still returns 0 (the merge already happened)"
assert_eq "" "$(read_recon)" "Unanswerable reconcile-plan -> reconcile-stack.sh NOT invoked"
assert_eq "" "$(read_gh_log)" "Unanswerable reconcile-plan -> no comment posted"
assert_eq "" "$(read_discovery)" \
  "Unanswerable reconcile-plan -> the gate skips BEFORE the gh pr list discovery query"

# T11: the same, with a pre-merge snapshot already in hand — the skip must not
# depend on which rollup source was used.
reset_logs
PR_BRANCH="feature/issue-100"
STACKED_CHILDREN_JSON='[{"number":501,"headRefName":"feature/issue-201"}]'
with_daemon "$FAKE_DIR/daemon-no-verbs" 2>/dev/null || true
assert_eq "" "$(read_recon)" \
  "Unanswerable reconcile-plan with a pre-merge snapshot -> still no reconcile (a snapshot is not a verdict)"
unset STACKED_CHILDREN_JSON

# T12: the plan is answered but the CHILD verdict is not. The child that WOULD
# have reconciled must be skipped instead: not knowing whether issue #201 is
# still claimed is not the same as knowing it is not.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
unset STACKED_CHILDREN_JSON 2>/dev/null || true
with_daemon "$FAKE_DIR/daemon-no-child" 2>/dev/null || true
assert_eq "" "$(read_recon)" \
  "Unanswerable reconcile-child -> the safe-looking child is NOT force-pushed on a guess"
assert_eq "" "$(read_gh_log)" \
  "Unanswerable reconcile-child -> no deferral comment either (an unknown verdict is not a deferral)"

# T13: exit 0 is not consent. A daemon that succeeds but prints something other
# than a verdict — a warning line, a truncated write — must still skip.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
with_daemon "$FAKE_DIR/daemon-child-garbage" 2>/dev/null || true
assert_eq "" "$(read_recon)" \
  "reconcile-child exiting 0 with a non-verdict -> still no reconcile (status alone is not consent)"

# T14: the pin is restored — every later assertion, and every rerun of T1-T9,
# must still be measuring the real binary rather than a leaked fake.
reset_logs
PR_BRANCH="feature/issue-100"
write_prlist "feature/issue-100" '[{"number":501,"headRefName":"feature/issue-201"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 501 feature/issue-100" \
  "The real daemon pin survives the fail-open cases (no leaked LOOM_DAEMON_SELF_BIN)"

# --- T15-T17 (#1298): feature/harness-ops-<N> is a stackable parent too ---
#
# Both decisions (the parent-branch gate in `reconcile-plan`, the child-issue
# derivation in `reconcile-child`) route through `reconcile::issue_from_branch`
# (loom-daemon/src/merge_pr/reconcile.rs), which now recognizes
# `feature/harness-ops-<N>` (2AMLogic/harness-ops's Builder convention)
# alongside `feature/issue-<N>` — mirroring `stacked_children::
# is_stackable_parent_branch`'s allow-list so the pre-merge and post-merge
# gates can never disagree. Before this, a harness-ops parent merge silently
# skipped stacked-child reconciliation and stranded open children
# (harness-ops#283, #356). These exercise the real daemon binary end-to-end
# through the shell functions under test, same as T1-T9.

# T15: a feature/harness-ops-<N> parent with an open, unclaimed child ->
# reconcile-stack.sh invoked.
reset_logs
PR_BRANCH="feature/harness-ops-350"
write_prlist "feature/harness-ops-350" '[{"number":601,"headRefName":"feature/harness-ops-201"}]'
_auto_reconcile_stacked_children
assert_contains "$(read_recon)" "reconcile-stack.sh 601 feature/harness-ops-350" \
  "feature/harness-ops-N parent with an open child -> reconcile-stack.sh invoked (#1298)"
assert_eq "" "$(read_gh_log)" "harness-ops safe child (issue #201 not building) -> no deferred comment"

# T16: a feature/harness-ops-<N> CHILD whose issue is loom:building is
# recognized as claimed and deferred, not rebased out from under a live Builder.
reset_logs
PR_BRANCH="feature/harness-ops-350"
write_prlist "feature/harness-ops-350" '[{"number":602,"headRefName":"feature/harness-ops-202"}]'
_auto_reconcile_stacked_children
assert_eq "" "$(read_recon)" "harness-ops child #602 (issue #202 building) -> reconcile-stack.sh NOT invoked"
assert_contains "$(read_gh_log)" "issue comment 602 --repo owner/repo" \
  "harness-ops building child -> deferred-reconciliation comment posted on PR #602"

# T17: the generalized match stays strict/anchored — near-miss parent branches
# are still NOT-STACKED.
reset_logs
for b in "feature/harness-ops-" "feature/harness-ops-350-extra" "feature/harness-ops-350/sub" \
         "feature/issue-100-extra" "feature/other-350"; do
  write_prlist "$b" '[{"number":603,"headRefName":"feature/issue-201"}]'
  PR_BRANCH="$b"
  _auto_reconcile_stacked_children
done
assert_eq "" "$(read_recon)" "Non-matching near-miss parent branches -> reconcile skipped"

# --- Source-contains guards (fail if a refactor drops the key behavior) ---
echo ""
echo "Testing merge-pr.sh source guards..."
src="$(cat "$MERGE_PR_SRC")"
assert_contains "$src" "_auto_reconcile_stacked_children" \
  "merge-pr.sh defines and calls _auto_reconcile_stacked_children"
assert_contains "$src" "_auto_reconcile_stacked_children || true" \
  "merge-pr.sh invokes the reconcile step best-effort (|| true) at the merge choke point"
assert_contains "$src" 'gh pr list --repo "$REPO_NWO" --base "$PR_BRANCH" --state open' \
  "merge-pr.sh discovers children via a live forge query, not the daemon registry"
assert_contains "$src" 'local children_json="${STACKED_CHILDREN_JSON:-}"' \
  "merge-pr.sh's post-merge reconcile prefers the pre-merge STACKED_CHILDREN_JSON snapshot (#8010 item 2)"
retired "source grep for \"grep -qx 'loom:building'\" in merge-pr.sh" \
  "safe/unsafe is gated on the child issue's loom:building label, matched as a WHOLE LINE (grep -qx) so a different label that merely contains the name cannot authorise a deferral or a rebase" \
  "the grep is gone — the gate is loom-daemon merge-pr reconcile-child (merge_pr::reconcile::child_route), so there is no grep invocation in merge-pr.sh left to assert the flags of, and a source grep for one can only ever fail" \
  "T3/T7 above still prove the label DECIDES the route end-to-end through the real binary; merge_pr::reconcile::tests::the_claim_match_is_whole_line_not_substring pins the -x half against loom:building-paused, ' loom:building', 'LOOM:BUILDING' and 'loom:build'; and merge_pr_reconcile_differential.rs replays a label corpus against the frozen retired grep -qx itself"
assert_contains "$src" '"$SCRIPT_DIR/reconcile-stack.sh" "$child_pr" "$parent_branch"' \
  "merge-pr.sh reuses reconcile-stack.sh unmodified (no inline rebase logic)"
assert_contains "$src" 'forge_gh_comment_rl_safe "$REPO_NWO" "$child_pr" "$comment"' \
  "merge-pr.sh posts the deferral comment via the rate-limit-safe wrapper (#4856)"

# --- Summary ---
echo ""
echo "────────────────────────────────"
echo "Results: $TESTS_PASSED/$TESTS_RUN passed, $TESTS_FAILED failed"

if [[ $TESTS_FAILED -gt 0 ]]; then
    exit 1
fi
exit 0
