#!/usr/bin/env bash
# test-guide-work-log-rotation.sh - Regression test for issue #12634
# (slice 2 of #12589: Guide runs WORK_LOG rotation inside Document Maintenance).
#
# Executes the prompt's OWN code, extracted from guide.md, against a throwaway
# git repo and a stub `loom-daemon work-docs` (the verb ships in a different
# binary than the one on a test host's PATH; its behavior is covered by the
# Rust tests from #12633). Verifies:
#   1. the lookback-vs-floor invariant is stated next to work_log_has_pr() and
#      rotate_work_log() skips (never rotates) when it does not hold;
#   2. rotation is gated: nothing eligible -> no change; eligible but the tick
#      writes no WORK_LOG and under a whole month -> deferred, no change;
#   3. a tick that writes WORK_LOG rotates, and runs `work-docs check`, which
#      aborts the rotation (worktree restored) on non-zero;
#   4. dedup after rotation: the presence checks do not see archived entries,
#      so the cutoff must stay older than the lookback (fresh checkout with
#      archives does not make a recent candidate look unrecorded);
#   5. create_docs_pr() stages archive files by exact pattern only: an
#      unrelated work_log/notes.md is NOT staged;
#   6. repeated no-op ticks stage nothing; an interrupted tick (rotated, no PR)
#      is wiped by docs-worktree.sh's reset and the next tick redoes it
#      identically, with no gitignored sidecar involved.
# Hermetic: no forge or network.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
if [[ -f "$REPO_ROOT/.claude/commands/loom/guide.md" ]]; then
    GUIDE_MD="$REPO_ROOT/.claude/commands/loom/guide.md"
else
    GUIDE_MD="$REPO_ROOT/defaults/.claude/commands/loom/guide.md"
fi

RED='\033[0;31m'; GREEN='\033[0;32m'; NC='\033[0m'
TESTS_RUN=0; TESTS_FAILED=0
pass() { TESTS_RUN=$((TESTS_RUN + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }
assert_eq() { if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (got '$1', expected '$2')"; fi; }
assert_grep() { if grep -qE "$1" "$2"; then pass "$3"; else fail "$3 (missing: $1)"; fi; }

SANDBOX="$(mktemp -d)"
trap 'rm -rf "$SANDBOX"' EXIT

# --- extract the prompt's code verbatim ---------------------------------
extract_fn() { awk -v n="$1" '$0 ~ "^"n"\\(\\) \\{" {p=1} p {print} p && /^}/ {exit}' "$GUIDE_MD"; }
extract_var() { grep -m1 "^$1=" "$GUIDE_MD"; }

LIB="$SANDBOX/lib.sh"
{
    extract_var WORK_LOG_LOOKBACK_DAYS
    extract_var WORK_LOG_ROTATE_OLDER_THAN_DAYS
    extract_var WORK_LOG_ROTATE_MARGIN_DAYS
    extract_var WORK_LOG_ARCHIVE_GLOB
    extract_fn work_log_has_pr
    extract_fn work_log_has_issue
    extract_fn rotate_work_log
} > "$LIB"
# The staging block of create_docs_pr(), between the archive comment and `done`.
{ echo 'stage_archives() {'; awk '/local archive_file/ {p=1} p {print} p && /^  done/ {exit}' "$GUIDE_MD"; echo '}'; } > "$SANDBOX/stage.sh"
stage_tick() { (source "$LIB"; source "$SANDBOX/stage.sh"; git -C "$DOCS_WT" add WORK_LOG.md WORK_PLAN.md README.md; stage_archives); }

echo "Test 1: invariant stated and enforced"
assert_grep 'Lookback-versus-rotation-floor invariant' "$GUIDE_MD" "invariant documented next to the presence checks"
assert_grep 'date -u -d "\$\{WORK_LOG_LOOKBACK_DAYS\} days ago"' "$GUIDE_MD" "update_work_log() derives since from WORK_LOG_LOOKBACK_DAYS"
[[ -s "$SANDBOX/stage.sh" ]] && pass "staging block extracted" || fail "staging block extracted"
for fn in rotate_work_log work_log_has_pr; do
    [[ -n "$(extract_fn $fn)" ]] && pass "$fn extracted" || fail "$fn extracted"
done

# --- stub daemon ---------------------------------------------------------
BIN="$SANDBOX/bin"; mkdir -p "$BIN"
cat > "$BIN/loom-daemon" <<'STUB'
#!/usr/bin/env bash
# args: work-docs archive|check --root DIR [--older-than-days N] [--dry-run]
sub="$2"; root=""; dry=0
while [[ $# -gt 0 ]]; do case "$1" in --root) root="$2"; shift;; --dry-run) dry=1;; esac; shift; done
echo "$sub dry=$dry" >> "$STUB_CALLS"
if [[ "$sub" == check ]]; then exit "${STUB_CHECK_RC:-0}"; fi
if [[ "$dry" == 1 ]]; then echo "dry run: ${STUB_MOVED:-0} sections older than 2026-01-01 -> 1 archive file(s); WORK_LOG.md: 9 -> 5 lines"; exit "${STUB_DRY_RC:-0}"; fi
# real run: move the oldest section into the July archive
mkdir -p "$root/work_log"
printf '# Work Log Archive — 2026-07\n\n### 2026-07-01\n\n- **PR #1**: old\n' > "$root/work_log/WORK_LOG_2026-07.md"
printf '# Work Log\n\n### 2026-10-01\n\n- **PR #9**: recent\n' > "$root/WORK_LOG.md"
STUB
chmod +x "$BIN/loom-daemon"
export PATH="$BIN:$PATH" STUB_CALLS="$SANDBOX/calls"

# --- origin + docs worktree ---------------------------------------------
ORIGIN="$SANDBOX/origin.git"; DOCS_WT="$SANDBOX/wt"
git init -q --bare "$ORIGIN"
git clone -q "$ORIGIN" "$DOCS_WT" 2>/dev/null
git -C "$DOCS_WT" config user.email t@t; git -C "$DOCS_WT" config user.name t
printf '# Work Log\n\n### 2026-10-01\n\n- **PR #9**: recent\n\n### 2026-07-01\n\n- **PR #1**: old\n' > "$DOCS_WT/WORK_LOG.md"
echo plan > "$DOCS_WT/WORK_PLAN.md"; echo readme > "$DOCS_WT/README.md"
git -C "$DOCS_WT" add -A; git -C "$DOCS_WT" commit -qm base; git -C "$DOCS_WT" push -q origin HEAD:main 2>/dev/null
export DOCS_WT
# docs-worktree.sh's per-tick reset (reset --hard + clean -fd), as it does on the reused slot.
reset_wt() { git -C "$DOCS_WT" reset --hard --quiet HEAD; git -C "$DOCS_WT" clean -qfd; }
snapshot() { (cd "$DOCS_WT" && find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 cat | sha256sum | cut -d' ' -f1); }
run() { # run <wrote> in a subshell with the extracted lib
    (source "$LIB"; rotate_work_log "$1") >"$SANDBOX/out" 2>&1; echo $?
}

echo "Test 2: guard skips when the cutoff is not older than lookback + margin"
: > "$STUB_CALLS"; BASE="$(snapshot)"
# shellcheck disable=SC2034  # read by rotate_work_log in the sourced lib
rc="$( (source "$LIB"; WORK_LOG_LOOKBACK_DAYS=50; STUB_MOVED=40 rotate_work_log 1) >"$SANDBOX/out" 2>&1; echo $? )"
assert_eq "$rc" "1" "rotation refused when lookback (50d) + margin exceeds the 60d cutoff"
assert_grep 'rotation skipped' "$SANDBOX/out" "a warning is logged"
assert_eq "$(wc -l < "$STUB_CALLS" | tr -d ' ')" "0" "the verb was never invoked"

echo "Test 3: gating"
: > "$STUB_CALLS"
assert_eq "$(STUB_MOVED=0 run 1)" "1" "nothing eligible: no rotation"
assert_eq "$(STUB_MOVED=3 run 0)" "1" "eligible but tick writes nothing and < a month: deferred"
assert_grep 'deferred' "$SANDBOX/out" "deferral logged"
assert_eq "$(STUB_DRY_RC=1 run 1)" "1" "dry-run failure (e.g. daemon without work-docs) skips rotation"
assert_eq "$(grep -c 'dry=0' "$STUB_CALLS")" "0" "no real archive run in any of those"
assert_eq "$(snapshot)" "$BASE" "worktree untouched (a rotation-only tick cannot produce a diff)"
assert_eq "$(STUB_MOVED=30 run 0)" "0" "a whole month eligible rotates even if the tick writes no WORK_LOG"
reset_wt

echo "Test 4: writing tick rotates; check failure aborts and restores"
: > "$STUB_CALLS"
assert_eq "$(STUB_MOVED=2 run 1)" "0" "tick that writes WORK_LOG rotates"
assert_eq "$(grep -c '^check' "$STUB_CALLS")" "1" "work-docs check ran after the move"
[[ -f "$DOCS_WT/work_log/WORK_LOG_2026-07.md" ]] && pass "archive written" || fail "archive written"
reset_wt; BASE="$(snapshot)"
assert_eq "$(STUB_MOVED=2 STUB_CHECK_RC=1 run 1)" "1" "check failure aborts rotation"
assert_eq "$(snapshot)" "$BASE" "worktree restored after the aborted rotation"

echo "Test 5: dedup stays correct across a fresh checkout that has archives"
# Active file keeps everything within the cutoff; archived PR #1 is far older than the lookback.
STUB_MOVED=2 run 1 >/dev/null
(source "$LIB"; work_log_has_pr 9) && pass "recent PR #9 still recorded in the active file (not re-appended)" || fail "recent PR #9 still recorded"
(source "$LIB"; work_log_has_pr 1) && fail "archived PR #1 unexpectedly visible" || pass "archived PR #1 is invisible to the presence check (safe only because it is older than the lookback)"
reset_wt

echo "Test 6: staging by exact pattern, repeat no-op ticks, interrupted tick"
STUB_MOVED=2 run 1 >/dev/null
echo stray > "$DOCS_WT/work_log/notes.md"
echo stray > "$DOCS_WT/work_log/WORK_LOG_2026-7.md"
stage_tick
STAGED="$(git -C "$DOCS_WT" diff --cached --name-only | sort | paste -sd, -)"
assert_eq "$STAGED" "WORK_LOG.md,work_log/WORK_LOG_2026-07.md" "only WORK_LOG.md and the pattern-matching archive are staged"
# Interrupted tick: rotation done, PR never created. The next tick's reset wipes it...
FIRST="$(git -C "$DOCS_WT" diff --cached | sha256sum | cut -d' ' -f1)"
reset_wt
assert_eq "$([[ -e $DOCS_WT/work_log/WORK_LOG_2026-07.md ]] && echo yes || echo no)" "no" "reset leaves no half-rotated archive behind"
# ...and the next tick redoes the identical rotation.
STUB_MOVED=2 run 1 >/dev/null
stage_tick
SECOND="$(git -C "$DOCS_WT" diff --cached | sha256sum | cut -d' ' -f1)"
assert_eq "$SECOND" "$FIRST" "resumed tick stages the identical rotation"
reset_wt
# Repeated no-op tick: nothing eligible -> nothing staged -> create_docs_pr's diff --cached --quiet path.
STUB_MOVED=0 run 1 >/dev/null
stage_tick
git -C "$DOCS_WT" diff --cached --quiet && pass "no-op tick stages nothing (no docs PR)" || fail "no-op tick stages nothing"

echo ""
echo "Tests run: $TESTS_RUN, failed: $TESTS_FAILED"
[[ "$TESTS_FAILED" -eq 0 ]]
