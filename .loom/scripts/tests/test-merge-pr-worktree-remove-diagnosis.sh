#!/usr/bin/env bash
# test-merge-pr-worktree-remove-diagnosis.sh - Regression test for #6372.
#
# merge-pr.sh's post-merge worktree cleanup (`_remove_loom_worktree`) used to
# handle a failed `git worktree remove --force` with nothing but a bare
# `warning "Could not remove worktree at $worktree_path"` — the actual git
# error was discarded (`2>/dev/null`), there was no remediation command, and
# no attempt was made to recover from a stale worktree registration before
# giving up. This reads as a "Done" success (the merge itself did succeed;
# only cleanup failed) with an unexplained warning tacked on, which is exactly
# the "exit code disagrees with the message" complaint #6372 reported.
#
# A sibling code path in the SAME function (the partial-increment /
# issue-state-lookup case) already sets the bar: it names the case and the
# remedy explicitly. This test locks in that #6372 brings worktree-removal
# failure up to the same standard:
#   1. The first removal attempt's actual git error is captured and surfaced
#      (no more silent 2>/dev/null discard).
#   2. On failure, `git worktree prune` is tried once and the removal is
#      retried — a stale worktree registration (administrative metadata out
#      of sync with the actual directory, the #6372 reporter's own working
#      hypothesis, confirmed by their manual `git worktree prune` recovery)
#      can make the first attempt fail even though nothing is genuinely
#      holding the worktree open.
#   3. If the retry also fails, the warning states the failure is best-effort
#      (the merge already succeeded and is unaffected), includes the actual
#      git error, and names the remediation commands — never a bare
#      "could not remove" with no explanation.
#   4. Exit status matches the "best-effort" decision: `_remove_loom_worktree`
#      never calls error()/exit 1 on a removal failure, so the containing
#      merge-pr.sh run still reports success for the merge itself.
#
# Test strategy (mirrors test-merge-pr-dirty-worktree-guard.sh /
# test-merge-pr-primary-worktree-guard.sh):
#   1. Static grep assertions that the live source retains the prune-and-retry
#      logic, the captured error, and the remediation message.
#   2. Behavioral tests running the ACTUAL function body extracted from
#      merge-pr.sh (no drift) against REAL git repos, with `git` itself
#      swapped out via PATH for a thin wrapper that deterministically forces
#      `git worktree remove --force` to fail on its first call — a corrupt
#      worktree registration is otherwise awkward to reproduce portably, and
#      this lets the test assert on the retry behavior precisely rather than
#      relying on incidental host git behavior:
#      (a) first attempt fails, `git worktree prune` is invoked, second
#          attempt succeeds -> "removed after pruning" success message, the
#          worktree is actually gone.
#      (b) both attempts fail -> best-effort warning naming the real git
#          error and the remediation commands, rc 0, worktree still present.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
MERGE_PR="$SCRIPTS_DIR/merge-pr.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_grep() {
    local pattern="$1" file="$2" msg="$3"
    if grep -qE "$pattern" "$file"; then pass "$msg"; else fail "$msg (pattern: $pattern)"; fi
}

[[ -f "$MERGE_PR" ]] || { echo "ERROR: $MERGE_PR not found" >&2; exit 1; }

# --- Test 1: source retains the #6372 diagnosis + prune-and-retry logic ---
echo "Test 1: merge-pr.sh source retains the #6372 worktree-removal diagnosis"

assert_grep '#6372' "$MERGE_PR" \
    "_remove_loom_worktree documents the #6372 fix"
assert_grep 'worktree remove "\$worktree_path" --force 2>&1' "$MERGE_PR" \
    "the first removal attempt captures stderr instead of discarding it"
assert_grep 'git -C "\$REPO_ROOT" worktree prune' "$MERGE_PR" \
    "a prune is attempted before giving up"
assert_grep 'Could not remove worktree at \$worktree_path \(best-effort cleanup' "$MERGE_PR" \
    "the terminal failure message states the best-effort decision"
assert_grep 'Remediation: git worktree prune' "$MERGE_PR" \
    "the terminal failure message names the remediation command"
assert_grep 'removed \(after pruning a stale worktree registration\)' "$MERGE_PR" \
    "a successful prune-then-retry is reported distinctly from a first-try success"

# --- Extract the ACTUAL function bodies from the live source (no drift) ---
extract_fn() {
    local name="$1" file="$2"
    awk -v fn="$name" '
      $0 ~ "^"fn"\\(\\) \\{" { grab=1 }
      grab { print }
      grab && /^}/ { exit }
    ' "$file"
}

# Harness: stub the logging helpers and REPO_ROOT, then eval the real bodies.
info()    { echo "INFO: $*"; }
warning() { echo "WARN: $*"; }
success() { echo "OK: $*"; }
error()   { echo "ERROR: $*" >&2; return 1; }
loom_record_worktree_removal() { :; }  # no-op stub; ledger writes are out of scope here

eval "$(extract_fn _primary_worktree_path "$MERGE_PR")"
eval "$(extract_fn _worktree_branch_for   "$MERGE_PR")"
# #7812: _maybe_delete_local_branch's `-d` -> `-D` safety check is now the
# shared `branch_landed` primitive — a real library, so it is SOURCED here
# rather than extracted. Offline: these cases exercise merge-pr.sh's local
# branch logic, not the forge rung, and the suite must stay hermetic.
export LOOM_BRANCH_LANDED_OFFLINE=1
# shellcheck source=../lib/branch-landed.sh
source "$(dirname "$MERGE_PR")/lib/branch-landed.sh"
eval "$(extract_fn _maybe_delete_local_branch "$MERGE_PR")"
eval "$(extract_fn _remove_loom_worktree  "$MERGE_PR")"

# --- Build a real git repo with a secondary Loom-managed worktree ---
TMP_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/loom-merge-remove-diag.XXXXXX")"
TMP_ROOT="$(cd "$TMP_ROOT" && pwd -P)"
cleanup() { rm -rf "$TMP_ROOT" 2>/dev/null || true; }
trap cleanup EXIT

PRIMARY="$TMP_ROOT/repo"
mkdir -p "$PRIMARY"
git -C "$PRIMARY" init -q
git -C "$PRIMARY" config user.email "test@example.com"
git -C "$PRIMARY" config user.name "Test"
echo "hello" > "$PRIMARY/README.md"
git -C "$PRIMARY" add -A
git -C "$PRIMARY" commit -q -m "initial"
git -C "$PRIMARY" branch -M main

# shellcheck disable=SC2034
REPO_ROOT="$PRIMARY"

make_worktree() { # <name> -> echoes the worktree path
    local name="$1"
    local wt="$TMP_ROOT/wt-$name"
    git -C "$PRIMARY" worktree add -q -b "feature/issue-$name" "$wt" >/dev/null 2>&1
    touch "$wt/.loom-managed"
    echo "$wt"
}

# Locate real git once, before any PATH manipulation, so the fake wrapper can
# always delegate to it by absolute path (never itself, no recursion risk).
REAL_GIT="$(command -v git)"

# make_fake_git <bin-dir> <fail-count> <marker-file> <error-text>
#
# Writes a `git` wrapper into <bin-dir> that intercepts ONLY
# `worktree remove ... --force` calls: the first <fail-count> such calls print
# <error-text> to stderr and exit 128 (git's typical fatal-error status);
# every call after that delegates to the real `git worktree remove` so the
# worktree is genuinely removed. A `worktree prune` call is logged to
# <marker-file> (so the test can assert it was actually invoked) and then
# delegated to real git. Every other invocation passes straight through.
make_fake_git() {
    local bin_dir="$1" fail_count="$2" marker_file="$3" error_text="$4"
    mkdir -p "$bin_dir"
    local counter_file="$bin_dir/.remove-attempts"
    echo 0 > "$counter_file"
    cat > "$bin_dir/git" <<WRAPPER
#!/usr/bin/env bash
if [[ "\$*" == *"worktree remove"* && "\$*" == *"--force"* ]]; then
    n=\$(cat "$counter_file")
    n=\$((n + 1))
    echo "\$n" > "$counter_file"
    if [[ "\$n" -le $fail_count ]]; then
        echo "$error_text" >&2
        exit 128
    fi
    exec "$REAL_GIT" "\$@"
fi
if [[ "\$*" == *"worktree prune"* ]]; then
    echo "pruned" >> "$marker_file"
    exec "$REAL_GIT" "\$@"
fi
exec "$REAL_GIT" "\$@"
WRAPPER
    chmod +x "$bin_dir/git"
}

# --- Test 2 (a): first attempt fails, prune fixes it, retry succeeds ---
echo ""
echo "Test 2: a first-attempt removal failure recovers via prune-and-retry"

WT_A="$(make_worktree 6372a)"
FAKE_BIN_A="$TMP_ROOT/fake-bin-a"
MARKER_A="$TMP_ROOT/prune-marker-a"
make_fake_git "$FAKE_BIN_A" 1 "$MARKER_A" "fatal: simulated stale worktree registration"

set +e
out_a="$(PATH="$FAKE_BIN_A:$PATH" _remove_loom_worktree "$WT_A" 2>&1)"
rc_a=$?
set -e

if [[ $rc_a -eq 0 ]] \
   && [[ -f "$MARKER_A" ]] \
   && [[ "$out_a" == *"removed (after pruning a stale worktree registration)"* ]] \
   && [[ "$out_a" != *"Could not remove worktree"* ]] \
   && [[ ! -d "$WT_A" ]]; then
    pass "(a) prune-and-retry recovers a stale-registration failure and removes the worktree"
else
    fail "(a) expected prune-then-succeed; rc=$rc_a, pruned=$([[ -f "$MARKER_A" ]] && echo yes || echo no), dir=$([[ -d "$WT_A" ]] && echo yes || echo no), out: $out_a"
fi

# --- Test 3 (b): both attempts fail -> best-effort diagnosis + remediation ---
echo ""
echo "Test 3: a removal failure that survives prune gets a diagnosed, best-effort warning"

WT_B="$(make_worktree 6372b)"
FAKE_BIN_B="$TMP_ROOT/fake-bin-b"
MARKER_B="$TMP_ROOT/prune-marker-b"
make_fake_git "$FAKE_BIN_B" 99 "$MARKER_B" "fatal: simulated persistent removal failure"

set +e
out_b="$(PATH="$FAKE_BIN_B:$PATH" _remove_loom_worktree "$WT_B" 2>&1)"
rc_b=$?
set -e

if [[ $rc_b -eq 0 ]] \
   && [[ -f "$MARKER_B" ]] \
   && [[ "$out_b" == *"Could not remove worktree at $WT_B (best-effort cleanup"* ]] \
   && [[ "$out_b" == *"simulated persistent removal failure"* ]] \
   && [[ "$out_b" == *"Remediation: git worktree prune"* ]] \
   && [[ -d "$WT_B" ]]; then
    pass "(b) a removal failure that survives prune reports a best-effort diagnosis with the real git error and a remediation command; rc stays 0"
else
    fail "(b) expected diagnosed best-effort warning; rc=$rc_b, pruned=$([[ -f "$MARKER_B" ]] && echo yes || echo no), dir=$([[ -d "$WT_B" ]] && echo yes || echo no), out: $out_b"
fi

# --- Test 4: control — a clean worktree with a healthy git still removes on
#     the first attempt (no regression to the common case) ---
echo ""
echo "Test 4: control — a healthy removal (no wrapper) still succeeds on the first try"

WT_C="$(make_worktree 6372c)"

set +e
out_c="$(_remove_loom_worktree "$WT_C" 2>&1)"
rc_c=$?
set -e

if [[ $rc_c -eq 0 ]] \
   && [[ "$out_c" == *"Removing worktree"* ]] \
   && [[ "$out_c" == *"Worktree removed"* ]] \
   && [[ "$out_c" != *"after pruning"* ]] \
   && [[ "$out_c" != *"Could not remove"* ]] \
   && [[ ! -d "$WT_C" ]]; then
    pass "(c) the common healthy-removal case is unaffected by the #6372 change"
else
    fail "(c) expected a plain first-try success; rc=$rc_c, dir=$([[ -d "$WT_C" ]] && echo yes || echo no), out: $out_c"
fi

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
