#!/usr/bin/env bash
# test-guide-docs-pr-exclude-refresh.sh - Regression test for issue #6627
#
# `GUIDE_DOCS_PR_EXCLUDE` (the jq predicate that keeps Guide's Document
# Maintenance phase from recording its own docs-maintenance PRs in
# WORK_LOG.md, #5454) was loaded ONLY from whatever THIS repo's installed
# copy of guide.md already contained. A repo whose install predates an
# upstream fix to this filter (resync not yet run) silently reintroduces the
# #5454 self-loop the filter exists to prevent.
#
# A consumer repo (rjwalters/repo, public) fixed this locally by adding a
# `preflight_refresh_docs_pr_exclude()` pre-flight to its INSTALLED copy of
# guide.md (rjwalters/repo#280): re-verify against origin/main's copy of the
# same file, prefer origin's value on a mismatch, fail safe to the local
# value when offline. Because that patch lived only in the consumer's
# vendored copy, every `resync-installed.sh` run stripped it back out
# (restored after being lost in rjwalters/repo#391 and #398) -- a
# consumer-side patch to a vendored file cannot survive the mechanism whose
# whole job is to overwrite that file. The fix belongs upstream, in
# `defaults/roles/guide.md` itself.
#
# Verifies that:
#   1. guide.md defines `refresh_docs_pr_exclude_from_origin()` immediately
#      after the `GUIDE_DOCS_PR_EXCLUDE` assignment and invokes it
#      unconditionally, so both Step 2 (`update_work_log()`) and Step 3
#      (`update_work_plan()`) -- which both consume the same variable -- see
#      the refreshed value.
#   2. THE REGRESSION, executed rather than grepped: the function body
#      extracted VERBATIM from guide.md, run against real throwaway local git
#      repos (mirrors test-check-main-freshness.sh's fixture shape):
#        a. local value matches origin's -> no-op: no warning printed, the
#           variable is left exactly as it was.
#        b. local value differs from origin's -> origin's value wins, a
#           warning is printed to stderr, and the variable used by
#           update_work_log()/update_work_plan() reflects origin's.
#        c. origin is unreachable (fetch fails) -> falls back to the local
#           value, no warning, and the phase is never blocked (the function
#           always returns success).
#
# Hermetic: throwaway local git repos under mktemp -d, no live forge/network
# calls -- "unreachable" is simulated by pointing the `origin` remote at a
# nonexistent local path, not by touching the real network.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# guide.md is shipped (installed at .claude/commands/loom/guide.md), so
# resolve it the way each layout actually lays it out: the installed path
# first (consumer repos, and Loom's own dogfooded checkout), falling back
# to the defaults/ source-tree path (a bare source checkout with no
# .claude/commands/loom/ copy yet). See issue #6194 / #6241.
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    local actual="$1" expected="$2" msg="$3"
    if [[ "$actual" == "$expected" ]]; then pass "$msg"; else fail "$msg (got '$actual', expected '$expected')"; fi
}

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (missing pattern: $pattern)"; fi
}

if [[ ! -f "$GUIDE_MD" ]]; then
    echo -e "${RED}FATAL${NC}: guide.md not found at $GUIDE_MD"
    exit 1
fi

# git needs an identity in a clean CI environment.
export GIT_AUTHOR_NAME="test" GIT_AUTHOR_EMAIL="test@example.com"
export GIT_COMMITTER_NAME="test" GIT_COMMITTER_EMAIL="test@example.com"

WORKDIR="$(mktemp -d "${TMPDIR:-/tmp}/test-guide-docs-pr-exclude-refresh.XXXXXX")"
# shellcheck disable=SC2329  # invoked indirectly via the EXIT trap below
cleanup() { rm -rf "$WORKDIR" 2>/dev/null || true; }
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Test 1: guide.md wires the pre-flight in immediately after the assignment,
# and calls it unconditionally (so Step 2 AND Step 3 both see the refresh).
# ---------------------------------------------------------------------------
echo "Test 1: guide.md defines and invokes refresh_docs_pr_exclude_from_origin()"

assert_grep '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD" \
    "GUIDE_DOCS_PR_EXCLUDE is still defined as a single-line assignment"
assert_grep '^refresh_docs_pr_exclude_from_origin\(\) \{' "$GUIDE_MD" \
    "refresh_docs_pr_exclude_from_origin() is defined"
assert_grep '^refresh_docs_pr_exclude_from_origin$' "$GUIDE_MD" \
    "refresh_docs_pr_exclude_from_origin is invoked unconditionally (not gated behind a flag)"

# The pre-flight call must land BEFORE the variable's first consumer
# (last_work_log_write_epoch, immediately below it in Step 2).
DEF_LINE="$(grep -n '^GUIDE_DOCS_PR_EXCLUDE=' "$GUIDE_MD" | head -1 | cut -d: -f1)"
CALL_LINE="$(grep -n '^refresh_docs_pr_exclude_from_origin$' "$GUIDE_MD" | head -1 | cut -d: -f1)"
FIRST_USE_LINE="$(grep -n '^last_work_log_write_epoch() {' "$GUIDE_MD" | head -1 | cut -d: -f1)"
if [[ -n "$DEF_LINE" && -n "$CALL_LINE" && -n "$FIRST_USE_LINE" ]] \
    && (( DEF_LINE < CALL_LINE && CALL_LINE < FIRST_USE_LINE )); then
    pass "the pre-flight runs after the assignment and before the variable's first use"
else
    fail "the pre-flight is not positioned between the assignment and the first use (def=$DEF_LINE call=$CALL_LINE first_use=$FIRST_USE_LINE)"
fi

# ---------------------------------------------------------------------------
# Extract the function body VERBATIM from guide.md so this suite can never
# silently drift from the actual prompt text.
# ---------------------------------------------------------------------------
FUNC_BODY="$(sed -n '/^refresh_docs_pr_exclude_from_origin() {/,/^}/p' "$GUIDE_MD")"

if [[ -z "$FUNC_BODY" ]]; then
    echo -e "${RED}FATAL${NC}: could not extract refresh_docs_pr_exclude_from_origin() from guide.md"
    echo "================================"
    echo "Tests run:    $TESTS_RUN"
    exit 1
fi
pass "extracted refresh_docs_pr_exclude_from_origin() verbatim from guide.md"

# Load the extracted function into THIS shell.
eval "$FUNC_BODY"

# ---------------------------------------------------------------------------
# Fixture builder: a bare "origin" remote plus a working clone that carries
# an installed guide.md at .claude/commands/loom/guide.md (the pre-flight's
# first-priority resolution path). Both start out defining the SAME
# GUIDE_DOCS_PR_EXCLUDE value.
# ---------------------------------------------------------------------------
LOCAL_VALUE='((.headRefName // "") | startswith("docs/guide-update")) or (.title == "docs: Guide document maintenance update")'
ORIGIN_VALUE_DIFFERENT='((.headRefName // "") | startswith("docs/guide-update")) or (.title == "docs: Guide document maintenance update") or (.author.login == "loom-bot")'

write_role_file() {
    local dir="$1" value="$2"
    mkdir -p "$dir/.claude/commands/loom"
    {
        echo "# fixture guide.md"
        echo "GUIDE_DOCS_PR_EXCLUDE='${value}'"
    } > "$dir/.claude/commands/loom/guide.md"
}

make_fixture() {
    local origin="$WORKDIR/origin.git"
    local clone="$WORKDIR/clone"
    rm -rf "$origin" "$clone"

    git init --quiet --bare "$origin"
    git -C "$origin" symbolic-ref HEAD refs/heads/main >/dev/null 2>&1 || true

    local seed="$WORKDIR/seed"
    rm -rf "$seed"
    git init --quiet "$seed"
    git -C "$seed" checkout -q -b main
    write_role_file "$seed" "$LOCAL_VALUE"
    git -C "$seed" add .
    git -C "$seed" commit -q -m "seed"
    git -C "$seed" remote add origin "$origin"
    git -C "$seed" push -q origin main

    git clone -q "$origin" "$clone"
}

# ---------------------------------------------------------------------------
# Test 2: local value matches origin's -> no-op (no warning, value unchanged)
# ---------------------------------------------------------------------------
echo ""
echo "Test 2: local matches origin -> no-op"

make_fixture
CLONE="$WORKDIR/clone"

# Run inside a subshell `cd`ed into the clone (the pre-flight resolves paths
# relative to cwd, exactly like the running agent would). Capture the
# resulting GUIDE_DOCS_PR_EXCLUDE via a file since a subshell can't mutate
# this shell's variables.
STDERR_LOG="$WORKDIR/stderr-match.log"
( cd "$CLONE"; eval "$FUNC_BODY"; GUIDE_DOCS_PR_EXCLUDE="$LOCAL_VALUE"; refresh_docs_pr_exclude_from_origin; echo "$GUIDE_DOCS_PR_EXCLUDE" > "$WORKDIR/result-match.txt" ) 2>"$STDERR_LOG"
RESULT="$(cat "$WORKDIR/result-match.txt")"

assert_eq "$RESULT" "$LOCAL_VALUE" "value is unchanged when local already matches origin"
if [[ -s "$STDERR_LOG" ]]; then
    fail "no warning should be printed when local matches origin (got: $(cat "$STDERR_LOG"))"
else
    pass "no warning printed when local matches origin"
fi

# ---------------------------------------------------------------------------
# Test 3: local differs from origin -> origin wins, warning printed
# ---------------------------------------------------------------------------
echo ""
echo "Test 3: local differs from origin -> origin's value wins, warning printed"

make_fixture
CLONE="$WORKDIR/clone"
SEED="$WORKDIR/seed"

# Advance origin with a DIFFERENT GUIDE_DOCS_PR_EXCLUDE value (simulates an
# upstream fix merging after this clone's install was last resynced).
write_role_file "$SEED" "$ORIGIN_VALUE_DIFFERENT"
git -C "$SEED" add .
git -C "$SEED" commit -q -m "upstream fix to GUIDE_DOCS_PR_EXCLUDE"
git -C "$SEED" push -q origin main

STDERR_LOG="$WORKDIR/stderr-mismatch.log"
( cd "$CLONE"; eval "$FUNC_BODY"; GUIDE_DOCS_PR_EXCLUDE="$LOCAL_VALUE"; refresh_docs_pr_exclude_from_origin; echo "$GUIDE_DOCS_PR_EXCLUDE" > "$WORKDIR/result-mismatch.txt" ) 2>"$STDERR_LOG"
RESULT="$(cat "$WORKDIR/result-mismatch.txt")"

assert_eq "$RESULT" "$ORIGIN_VALUE_DIFFERENT" "origin's value wins on a mismatch"
if grep -qi "warning" "$STDERR_LOG"; then
    pass "a warning is printed to stderr on a mismatch"
else
    fail "expected a warning on stderr, got: $(cat "$STDERR_LOG")"
fi

# ---------------------------------------------------------------------------
# Test 4: origin unreachable -> falls back to the local value, no failure
# ---------------------------------------------------------------------------
echo ""
echo "Test 4: origin unreachable -> falls back to the local value without failing"

make_fixture
CLONE="$WORKDIR/clone"
# Point origin at a nonexistent local path so the fetch fails deterministically
# and fast -- no real network involved, but the exact same failure shape as
# an offline host or a revoked credential.
git -C "$CLONE" remote set-url origin "$WORKDIR/does-not-exist.git"

STDERR_LOG="$WORKDIR/stderr-unreachable.log"
set +e
( cd "$CLONE"; eval "$FUNC_BODY"; GUIDE_DOCS_PR_EXCLUDE="$LOCAL_VALUE"; refresh_docs_pr_exclude_from_origin; echo "$?" > "$WORKDIR/exit-unreachable.txt"; echo "$GUIDE_DOCS_PR_EXCLUDE" > "$WORKDIR/result-unreachable.txt" ) 2>"$STDERR_LOG"
set -e
RESULT="$(cat "$WORKDIR/result-unreachable.txt")"
EXIT_CODE="$(cat "$WORKDIR/exit-unreachable.txt")"

assert_eq "$RESULT" "$LOCAL_VALUE" "falls back to the local value when origin is unreachable"
assert_eq "$EXIT_CODE" "0" "the pre-flight never fails the phase when origin is unreachable"
if [[ -s "$STDERR_LOG" ]] && grep -qi "warning" "$STDERR_LOG"; then
    fail "no mismatch warning should be printed when origin was unreachable (got: $(cat "$STDERR_LOG"))"
else
    pass "no mismatch warning printed when origin is unreachable"
fi

# ---------------------------------------------------------------------------
# Test 5: missing `timeout` binary still degrades gracefully (bounded-fetch
# convention parity with check-main-freshness.sh).
# ---------------------------------------------------------------------------
echo ""
echo "Test 5: missing 'timeout' binary still works (falls through to a plain fetch)"

make_fixture
CLONE="$WORKDIR/clone"

# Shadow `command` so `command -v timeout` reports "not found", forcing the
# plain-fetch branch, inside an isolated subshell only.
STDERR_LOG="$WORKDIR/stderr-no-timeout.log"
(
    cd "$CLONE"
    eval "$FUNC_BODY"
    # shellcheck disable=SC2329  # invoked indirectly, from inside refresh_docs_pr_exclude_from_origin()
    command() {
        if [[ "$1" == "-v" && "$2" == "timeout" ]]; then
            return 1
        fi
        builtin command "$@"
    }
    GUIDE_DOCS_PR_EXCLUDE="$LOCAL_VALUE"
    refresh_docs_pr_exclude_from_origin
    echo "$GUIDE_DOCS_PR_EXCLUDE" > "$WORKDIR/result-no-timeout.txt"
) 2>"$STDERR_LOG"
RESULT="$(cat "$WORKDIR/result-no-timeout.txt")"
assert_eq "$RESULT" "$LOCAL_VALUE" "without 'timeout', a matching origin still no-ops cleanly"

# ---------------------------------------------------------------------------
echo ""
echo "================================"
echo "Tests run:    $TESTS_RUN"
echo -e "Tests passed: ${GREEN}${TESTS_PASSED}${NC}"
if [[ $TESTS_FAILED -gt 0 ]]; then
    echo -e "Tests failed: ${RED}${TESTS_FAILED}${NC}"
    exit 1
fi
echo "All tests passed"
exit 0
