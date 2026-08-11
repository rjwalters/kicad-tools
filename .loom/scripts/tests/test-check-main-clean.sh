#!/usr/bin/env bash
# test-check-main-clean.sh - Smoke tests for check-main-clean.sh
#
# Exercises the main-worktree contamination backstop (#3513). Each test runs
# against a throwaway temp git repo so the result is deterministic and
# independent of the host repo's pre-existing untracked files.
#
# Verified behavior:
#   - exit 0 when the main worktree is clean
#   - exit 0 when only a gitignored issue worktree exists under .loom/worktrees/
#   - exit 3 when the main worktree has a stray untracked file
#   - exit 3 when the main worktree has a staged change
#   - exit 3 even when invoked from INSIDE a worktree (resolves main correctly)
#   - exit 0 from inside a worktree when main is clean
#   - exit 0 / coherent output for --help
#   - exit 2 for an unknown argument
#   - --snapshot FILE records main's porcelain state (#3648)
#   - --baseline FILE ignores pre-existing dirt but flags genuinely-new changes
#   - missing baseline file falls back to whole-status hard-fail (fail-safe)
#   - no-arg invocation remains a byte-for-byte whole-status hard-fail (back-compat)
#   - Loom-owned transient state (.loom/sweep-checkpoint/, etc.) is excluded
#     internally even when the repo's .gitignore has drifted and omits it (#3778)
#   - --quarantine atomically stashes NEW dirt (tracked + untracked) to a rescue
#     ref, leaves main byte-identical to the baseline, preserves the full diff in
#     the stash, spares baselined dirt, and exits 4 (#4380)
#
# Usage:
#   ./.loom/scripts/tests/test-check-main-clean.sh

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPERS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SCRIPT="$HELPERS_DIR/check-main-clean.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_PASSED=$((TESTS_PASSED + 1))
    echo -e "  ${GREEN}PASS${NC}: $1"
}

fail() {
    TESTS_RUN=$((TESTS_RUN + 1))
    TESTS_FAILED=$((TESTS_FAILED + 1))
    echo -e "  ${RED}FAIL${NC}: $1"
}

# Run the script, capture its exit code into a global.
run_rc() {
    ( "$@" ) >/dev/null 2>&1
    RC=$?
}

# Create a throwaway git repo with one commit and a gitignored worktree dir.
make_repo() {
    local dir
    dir=$(mktemp -d)
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.t
    git -C "$dir" config user.name test
    printf '.loom/worktrees/\n.loom/sweep-checkpoint/\n' > "$dir/.gitignore"
    git -C "$dir" add .gitignore
    git -C "$dir" commit -q -m init
    echo "$dir"
}

# -------- Test 1: script exists and is executable --------
echo "Test 1: script exists and is executable"
if [[ -x "$SCRIPT" ]]; then
    pass "check-main-clean.sh is executable"
else
    fail "check-main-clean.sh is missing or not executable: $SCRIPT"
    echo "FAILED: $TESTS_FAILED/$TESTS_RUN"
    exit 1
fi

# -------- Test 2: clean main exits 0 --------
echo "Test 2: clean main worktree exits 0"
REPO=$(make_repo)
( cd "$REPO" && run_rc "$SCRIPT" ) && true
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 on clean main"; else fail "expected 0, got $RC"; fi

# -------- Test 3: gitignored issue worktree present is still clean --------
echo "Test 3: gitignored .loom/worktrees/ does not count as dirty"
mkdir -p "$REPO/.loom/worktrees/issue-1"
echo "scratch" > "$REPO/.loom/worktrees/issue-1/foo.txt"
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 with gitignored worktree files"; else fail "expected 0, got $RC"; fi

# -------- Test 4: stray untracked file makes main dirty (exit 3) --------
echo "Test 4: stray untracked file in main exits 3"
echo "stray" > "$REPO/stray.txt"
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 on untracked stray file"; else fail "expected 3, got $RC"; fi
rm -f "$REPO/stray.txt"

# -------- Test 5: staged change makes main dirty (exit 3) --------
echo "Test 5: staged change in main exits 3"
echo "content" > "$REPO/tracked.txt"
git -C "$REPO" add tracked.txt
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 on staged change"; else fail "expected 3, got $RC"; fi
git -C "$REPO" reset -q HEAD tracked.txt
rm -f "$REPO/tracked.txt"

# -------- Test 6: invoked from inside a worktree, main dirty -> exit 3 --------
echo "Test 6: detects dirty main from inside a worktree"
git -C "$REPO" worktree add -q .loom/worktrees/issue-99 -b feature/issue-99 2>/dev/null
echo "stray2" > "$REPO/stray2.txt"
( cd "$REPO/.loom/worktrees/issue-99" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "exit 3 from worktree when main dirty"; else fail "expected 3, got $RC"; fi

# -------- Test 7: clean main from inside a worktree -> exit 0 --------
echo "Test 7: clean main from inside a worktree exits 0"
rm -f "$REPO/stray2.txt"
( cd "$REPO/.loom/worktrees/issue-99" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 from worktree when main clean"; else fail "expected 0, got $RC"; fi

# -------- Test 8: --help exits 0 and prints usage --------
echo "Test 8: --help exits 0 with usage output"
out=$("$SCRIPT" --help 2>&1); RC=$?
if [[ "$RC" -eq 0 && "$out" == *"check-main-clean.sh"* ]]; then
    pass "--help prints usage and exits 0"
else
    fail "--help: expected 0 + usage text, got rc=$RC"
fi

# -------- Test 9: unknown argument exits 2 --------
echo "Test 9: unknown argument exits 2"
"$SCRIPT" --bogus >/dev/null 2>&1; RC=$?
if [[ "$RC" -eq 2 ]]; then pass "exit 2 on unknown argument"; else fail "expected 2, got $RC"; fi

# Cleanup
git -C "$REPO" worktree remove --force .loom/worktrees/issue-99 2>/dev/null || true
rm -rf "$REPO"

# ========================================================================
# Baseline / snapshot mode (#3648)
# ========================================================================

# -------- Test 10: --snapshot writes main's porcelain content, exits 0 --------
echo "Test 10: --snapshot records porcelain state and exits 0"
REPO=$(make_repo)
echo "preexisting" > "$REPO/preexisting.txt"    # pre-existing untracked dirt
# Snapshot lives in a gitignored per-sweep transient dir so it does not itself
# register as new dirt (mirrors the /loom:sweep wiring, #3648).
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 && -f "$SNAP" ]] && grep -q "preexisting.txt" "$SNAP"; then
    pass "--snapshot writes porcelain content and exits 0"
else
    fail "--snapshot: expected 0 + file containing preexisting.txt, got rc=$RC"
fi

# -------- Test 11: baseline + only pre-existing dirt -> exit 0 --------
echo "Test 11: baseline ignores pre-existing dirt (exit 0)"
( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then pass "exit 0 when only pre-existing dirt remains"; else fail "expected 0, got $RC"; fi

# -------- Test 12: baseline + one genuinely-new file -> exit 3, reports only new --------
echo "Test 12: baseline flags a genuinely-new file (exit 3)"
echo "contamination" > "$REPO/new-contamination.txt"
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] \
   && echo "$out" | grep -q "new-contamination.txt" \
   && ! echo "$out" | grep -q "preexisting.txt"; then
    pass "exit 3 flagging only the new path, not pre-existing dirt"
else
    fail "expected 3 reporting only new-contamination.txt, got rc=$RC; out=$out"
fi

# -------- Test 13: baseline + pre-existing persists AND new file appears --------
echo "Test 13: baseline offending list excludes pre-existing dirt"
# preexisting.txt and new-contamination.txt both present; only the new one should be flagged.
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
offending=$(echo "$out" | sed -n '/Offending changes:/,$p')
if [[ "$RC" -eq 3 ]] \
   && echo "$offending" | grep -q "new-contamination.txt" \
   && ! echo "$offending" | grep -q "preexisting.txt"; then
    pass "offending list contains only the new path"
else
    fail "expected offending list with only new path, got rc=$RC; offending=$offending"
fi
rm -f "$REPO/new-contamination.txt"

# -------- Test 14: missing baseline file -> fail-safe whole-status behavior --------
echo "Test 14: missing baseline file falls back to whole-status (fail-safe)"
# preexisting.txt is still dirty; with a missing baseline the check must hard-fail.
out=$( cd "$REPO" && "$SCRIPT" --baseline "$REPO/.loom/does-not-exist.txt" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] && echo "$out" | grep -qi "missing or unreadable"; then
    pass "missing baseline warns and hard-fails on pre-existing dirt"
else
    fail "expected 3 + fallback warning, got rc=$RC; out=$out"
fi

# -------- Test 15: back-compat -- no-arg check still hard-fails on any dirt --------
echo "Test 15: no-arg invocation is byte-for-byte hard-fail (back-compat)"
# preexisting.txt still present; the legacy no-arg path must exit 3 regardless of any snapshot.
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]]; then pass "no-arg exit 3 on pre-existing dirt (unchanged contract)"; else fail "expected 3, got $RC"; fi

# -------- Test 16: --snapshot / --baseline require a file argument (exit 2) --------
echo "Test 16: --snapshot and --baseline require a file argument"
"$SCRIPT" --snapshot >/dev/null 2>&1; RC1=$?
"$SCRIPT" --baseline >/dev/null 2>&1; RC2=$?
if [[ "$RC1" -eq 2 && "$RC2" -eq 2 ]]; then
    pass "exit 2 when --snapshot/--baseline missing file arg"
else
    fail "expected 2/2, got snapshot=$RC1 baseline=$RC2"
fi

rm -rf "$REPO"

# ========================================================================
# cwd-reset contamination stand-in: a NEW TRACKED-FILE change on main (#3719)
# ========================================================================
# Tests 12/13 exercise an *untracked* stray file. This case covers the shape
# builders actually hit: after a cwd reset a repo-relative Write lands a new
# SOURCE MODULE in the main worktree, and the builder `git add`s it — a staged
# (tracked) change, not just an untracked one. The baseline backstop must still
# flag it (exit 3, naming the path) while ignoring a change that was already
# recorded in the pre-sweep snapshot. This is the detection defense the issue's
# "test simulating a cwd-reset mid-build" AC retargets at (the prevention guard
# `guard-worktree-paths.sh` cannot fire on the Task-subagent path — see PR body).

# -------- Test 17: baseline flags a NEW staged source module (exit 3) --------
echo "Test 17: baseline flags a new tracked-file change, ignores a baselined one"
REPO=$(make_repo)
# A change that predates the sweep and IS captured in the snapshot: a staged file.
printf 'baseline = 1\n' > "$REPO/baseline_mod.py"
git -C "$REPO" add baseline_mod.py
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -ne 0 ]]; then fail "Test 17 setup: --snapshot expected 0, got $RC"; fi

# Simulate the cwd-reset trap: a NEW source module written to main root, staged.
printf 'def widget():\n    return 42\n' > "$REPO/stray_module.py"
git -C "$REPO" add stray_module.py

out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
offending=$(echo "$out" | sed -n '/Offending changes:/,$p')
if [[ "$RC" -eq 3 ]] \
   && echo "$offending" | grep -q "stray_module.py" \
   && ! echo "$offending" | grep -q "baseline_mod.py"; then
    pass "exit 3 naming the new staged module, ignoring the baselined change"
else
    fail "expected 3 reporting only stray_module.py, got rc=$RC; offending=$offending"
fi
rm -rf "$REPO"

# ========================================================================
# Stale-.gitignore drift: Loom-owned transient state excluded internally (#3778)
# ========================================================================
# Reproduces the rjwalters/anvil false positive: a consumer repo whose installed
# loom-managed .gitignore block predates newer Loom-owned entries (here it omits
# .loom/sweep-checkpoint/) surfaces the orchestrator's own checkpoint bookkeeping
# as an untracked path. The backstop must NOT flag that as builder contamination —
# check-main-clean.sh excludes known Loom-owned transient paths internally,
# regardless of the consumer's .gitignore currency, while still catching a real
# stray.

# Build a repo whose .gitignore has DRIFTED: it only ignores .loom/worktrees/,
# NOT the newer .loom/sweep-checkpoint/ (and friends).
make_repo_stale_gitignore() {
    local dir
    dir=$(mktemp -d)
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.t
    git -C "$dir" config user.name test
    printf '.loom/worktrees/\n' > "$dir/.gitignore"   # note: no sweep-checkpoint entry
    # A real consumer repo has committed files under .loom/ (scripts, config), so
    # git reports newly-untracked transients at full granularity
    # (?? .loom/sweep-checkpoint/) rather than collapsing the whole dir to
    # ?? .loom/. Commit a tracked .loom/ file so the fixture mirrors that.
    mkdir -p "$dir/.loom"
    echo '{}' > "$dir/.loom/config.json"
    git -C "$dir" add .gitignore .loom/config.json
    git -C "$dir" commit -q -m init
    echo "$dir"
}

# -------- Test 18: Loom transient present + stale .gitignore -> exit 0 --------
echo "Test 18: Loom-owned transient excluded despite stale .gitignore (exit 0)"
REPO=$(make_repo_stale_gitignore)
mkdir -p "$REPO/.loom/sweep-checkpoint"
echo '{"phase":"builder-done"}' > "$REPO/.loom/sweep-checkpoint/issue-1.json"
mkdir -p "$REPO/.loom/tokens"
echo "secret" > "$REPO/.loom/tokens/agent-1.token"
touch "$REPO/.loom-managed"
# Sanity: without the internal filter these WOULD show as untracked dirt.
if [[ -z "$(git -C "$REPO" status --porcelain)" ]]; then
    fail "Test 18 setup: expected untracked Loom transients in a stale-gitignore repo"
fi
( cd "$REPO" && "$SCRIPT" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]]; then
    pass "exit 0 with only Loom-owned transient state present (stale .gitignore)"
else
    fail "expected 0 (Loom transients filtered), got $RC"
fi

# -------- Test 19: Loom transient + real stray -> exit 3 naming only the stray --------
echo "Test 19: real stray still flagged, Loom transient not (exit 3)"
echo "real contamination" > "$REPO/stray_source.py"
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] \
   && echo "$out" | grep -q "stray_source.py" \
   && ! echo "$out" | grep -q "sweep-checkpoint" \
   && ! echo "$out" | grep -q ".loom-managed"; then
    pass "exit 3 naming the real stray, excluding Loom transients"
else
    fail "expected 3 reporting only stray_source.py, got rc=$RC; out=$out"
fi
rm -rf "$REPO"

# -------- Test 20: baseline mode also excludes a mid-sweep checkpoint --------
echo "Test 20: baseline mode ignores a checkpoint created after the snapshot (#3778)"
REPO=$(make_repo_stale_gitignore)
# Snapshot BEFORE the sweep writes any checkpoint (the checkpoint dir is created
# during the sweep, so it cannot be in the pre-sweep baseline).
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -ne 0 ]]; then fail "Test 20 setup: --snapshot expected 0, got $RC"; fi
# Now the orchestrator writes a checkpoint (its own bookkeeping) AND a builder
# genuinely contaminates main with a new source file.
echo '{"phase":"judge-done"}' > "$REPO/.loom/sweep-checkpoint/issue-1.json"
echo "def widget(): return 42" > "$REPO/leaked_module.py"
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] \
   && echo "$out" | grep -q "leaked_module.py" \
   && ! echo "$out" | grep -q "sweep-checkpoint"; then
    pass "exit 3 flags the real leak, ignores the mid-sweep checkpoint"
else
    fail "expected 3 reporting only leaked_module.py, got rc=$RC; out=$out"
fi
rm -rf "$REPO"

# ========================================================================
# Atomic quarantine of detected contamination (#4380)
# ========================================================================
# The "partial revert" failure mode: a builder contaminates main with BOTH a
# modified tracked file and a new untracked file, then restores only some of it
# by hand. `--quarantine` replaces that ad-hoc path with ONE `git stash push
# --include-untracked` covering every offending path, so main lands back exactly
# at the pre-contamination baseline and the full diff survives in a rescue ref.

# Build a repo with a committed source file plus a pre-existing (baselined) edit.
make_repo_with_source() {
    local dir
    dir=$(mktemp -d)
    git -C "$dir" init -q
    git -C "$dir" config user.email t@t.t
    git -C "$dir" config user.name test
    printf '.loom/worktrees/\n.loom/sweep-checkpoint/\n' > "$dir/.gitignore"
    printf 'original tracked content\n' > "$dir/tracked_source.py"
    git -C "$dir" add .gitignore tracked_source.py
    git -C "$dir" commit -q -m init
    echo "$dir"
}

echo "Test 21: --quarantine atomically rescues tracked + untracked contamination"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-run1.txt"

# Pre-existing operator dirt that predates the sweep — must survive untouched.
printf 'operator scratch\n' > "$REPO/operator_scratch.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -ne 0 ]]; then fail "Test 21 setup: --snapshot expected 0, got $RC"; fi

# Capture the exact pre-contamination state of main (content + porcelain).
BASELINE_PORCELAIN=$(git -C "$REPO" status --porcelain)
BASELINE_TRACKED=$(cat "$REPO/tracked_source.py")
BASELINE_SCRATCH=$(cat "$REPO/operator_scratch.txt")

# --- Contaminate: one MODIFIED TRACKED file + one UNTRACKED file ---
printf 'original tracked content\ncontaminating edit\n' > "$REPO/tracked_source.py"
printf 'def leaked():\n    return 42\n' > "$REPO/leaked_module.py"

out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" --quarantine \
        --label "run=RUNID-TEST issue=4380" 2>&1 ); RC=$?

# 21a: the check flags the contamination and reports a successful quarantine (exit 4)
if [[ "$RC" -eq 4 ]]; then
    pass "--quarantine exits 4 (quarantined, caller may continue)"
else
    fail "expected exit 4 from --quarantine, got $RC; out=$out"
fi

# 21b: exactly ONE structured log entry, naming the label and both paths
json_lines=$(printf '%s\n' "$out" | grep -c '"event":"main-clean.quarantine"' || true)
json_line=$(printf '%s\n' "$out" | grep '"event":"main-clean.quarantine"' | head -1)
if [[ "$json_lines" -eq 1 ]] \
   && [[ "$json_line" == *'"result":"quarantined"'* ]] \
   && [[ "$json_line" == *'run=RUNID-TEST issue=4380'* ]] \
   && [[ "$json_line" == *"tracked_source.py"* ]] \
   && [[ "$json_line" == *"leaked_module.py"* ]]; then
    pass "exactly one structured log entry, attributed and naming both paths"
else
    fail "expected 1 structured entry naming label + both paths, got $json_lines; line=$json_line"
fi

# 21c: main is byte-identical to the pre-contamination baseline
AFTER_PORCELAIN=$(git -C "$REPO" status --porcelain)
if [[ "$AFTER_PORCELAIN" == "$BASELINE_PORCELAIN" ]] \
   && [[ "$(cat "$REPO/tracked_source.py")" == "$BASELINE_TRACKED" ]] \
   && [[ ! -e "$REPO/leaked_module.py" ]]; then
    pass "main worktree is byte-identical to the pre-contamination baseline"
else
    fail "main not restored to baseline: porcelain='$AFTER_PORCELAIN'"
fi

# 21d: baselined (pre-existing) operator dirt was NOT swept up
if [[ -f "$REPO/operator_scratch.txt" ]] \
   && [[ "$(cat "$REPO/operator_scratch.txt")" == "$BASELINE_SCRATCH" ]]; then
    pass "pre-existing baselined dirt left untouched by the quarantine"
else
    fail "quarantine swept up pre-existing dirt (operator_scratch.txt)"
fi

# 21e: the rescue ref retains the FULL diff — tracked edit AND untracked file
STASH_LIST=$(git -C "$REPO" stash list)
STASH_DIFF=$(git -C "$REPO" stash show -p --include-untracked 'stash@{0}' 2>/dev/null \
             || git -C "$REPO" stash show -p 'stash@{0}' 2>/dev/null)
STASH_FILES=$(git -C "$REPO" stash show --name-only --include-untracked 'stash@{0}' 2>/dev/null || true)
if [[ "$STASH_LIST" == *"loom-quarantine: run=RUNID-TEST issue=4380"* ]] \
   && [[ "$STASH_DIFF" == *"contaminating edit"* ]] \
   && [[ "$STASH_FILES" == *"leaked_module.py"* ]]; then
    pass "rescue stash retains the full diff (tracked edit + untracked file)"
else
    fail "stash lost part of the diff: list='$STASH_LIST' files='$STASH_FILES'"
fi

# 21f: nothing was discarded — the untracked file is recoverable from the stash
git -C "$REPO" stash pop -q 2>/dev/null || true
if [[ -e "$REPO/leaked_module.py" ]] \
   && [[ "$(cat "$REPO/tracked_source.py")" == *"contaminating edit"* ]]; then
    pass "quarantine is a rescue, not a discard (stash pop restores everything)"
else
    fail "stash pop did not restore the quarantined contamination"
fi
rm -rf "$REPO"

# -------- Test 22: --quarantine on a clean main is a no-op (exit 0) --------
echo "Test 22: --quarantine on a clean main creates no stash (exit 0)"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-run2.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
out=$( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" --quarantine 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]] && [[ -z "$(git -C "$REPO" stash list)" ]]; then
    pass "exit 0 and no stash created when main is clean"
else
    fail "expected 0 + empty stash list, got rc=$RC; stash='$(git -C "$REPO" stash list)'"
fi
rm -rf "$REPO"

# -------- Test 23: --quarantine writes its entry to --log FILE --------
echo "Test 23: --quarantine appends the structured entry to --log FILE"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-run3.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
echo "leak" > "$REPO/leaked.txt"
LOGFILE="$REPO/.loom/logs/quarantine-test.log"
( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" --quarantine --label "run=R issue=1" \
    --log "$LOGFILE" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 4 && -f "$LOGFILE" ]] \
   && [[ "$(grep -c '"event":"main-clean.quarantine"' "$LOGFILE")" -eq 1 ]] \
   && grep -q '"result":"quarantined"' "$LOGFILE"; then
    pass "--log FILE receives exactly one structured entry"
else
    fail "expected 1 structured entry in $LOGFILE, got rc=$RC"
fi
rm -rf "$REPO"

# -------- Test 24: --quarantine with --snapshot is a usage error --------
echo "Test 24: --quarantine is rejected with --snapshot (exit 2)"
REPO=$(make_repo_with_source)
( cd "$REPO" && "$SCRIPT" --snapshot "$REPO/snap.txt" --quarantine >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -eq 2 ]]; then
    pass "exit 2 for --snapshot --quarantine"
else
    fail "expected 2, got $RC"
fi

# -------- Test 25: detection-only mode still exits 3 and forbids piecemeal restore --------
echo "Test 25: without --quarantine, detection stays a hard-fail (exit 3)"
echo "leak" > "$REPO/leaked.txt"
out=$( cd "$REPO" && "$SCRIPT" 2>&1 ); RC=$?
if [[ "$RC" -eq 3 ]] \
   && echo "$out" | grep -qi "ALL-OR-NOTHING" \
   && echo "$out" | grep -q -- "--quarantine"; then
    pass "exit 3 with all-or-nothing remediation guidance (no piecemeal restore)"
else
    fail "expected 3 + all-or-nothing guidance, got rc=$RC; out=$out"
fi
rm -rf "$REPO"

# -------- Test 26: --label / --log require a value (exit 2) --------
echo "Test 26: --label and --log require a value"
"$SCRIPT" --label >/dev/null 2>&1; RC1=$?
"$SCRIPT" --log >/dev/null 2>&1; RC2=$?
if [[ "$RC1" -eq 2 && "$RC2" -eq 2 ]]; then
    pass "exit 2 when --label/--log missing their value"
else
    fail "expected 2/2, got label=$RC1 log=$RC2"
fi

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
