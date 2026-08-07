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
#   - --quarantine re-derives the offending paths immediately before the stash
#     push, so dirt resolved concurrently produces a `no_op` result and NO empty
#     stash entry; a push that creates no new entry is never reported as a
#     successful quarantine naming the previous entry's sha (#5185)
#   - --list-quarantined reports outstanding Loom-produced stash entries
#     (human + --json), covers every Loom producer, ignores human stashes, flags
#     empty entries, and always exits 0 (#5185)
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

# ========================================================================
# Quarantine race: re-derive offending paths before the push (#5185)
# ========================================================================
# The offending path set used to be computed from ONE early `git status`
# snapshot and then handed to `git stash push` much later. On a busy host the
# dirt could be resolved in between (a concurrent sweep, the builder's own
# commit, a cleanup pass) — and the stale pathspec then produced either an
# EMPTY stash entry (pure noise on a stash stack shared by every worktree,
# burying the real entries) or a no-op push that the success path still
# reported as "quarantined", naming whatever sha happened to be on top of the
# stack. Both are reproduced here with a `git` shim that injects the race
# deterministically.

# Write a `git` shim into $1/git that counts `status --porcelain` invocations
# in $1/count and runs $1/inject.sh just before the Nth one ($2), then execs
# the real git. Lets a test resolve the contamination in exactly the window
# between detection and the stash push.
make_git_shim() {
    local bindir="$1" nth="$2" real
    real=$(command -v git)
    mkdir -p "$bindir"
    echo 0 > "$bindir/count"
    cat > "$bindir/git" <<SHIM
#!/usr/bin/env bash
REAL="$real"
BINDIR="$bindir"
NTH="$nth"
for a in "\$@"; do
    if [[ "\$a" == "--porcelain" ]]; then
        n=\$(( \$(cat "\$BINDIR/count") + 1 ))
        echo "\$n" > "\$BINDIR/count"
        if [[ "\$n" -eq "\$NTH" && -x "\$BINDIR/inject.sh" ]]; then
            "\$BINDIR/inject.sh"
        fi
        break
    fi
done
exec "\$REAL" "\$@"
SHIM
    chmod +x "$bindir/git"
}

echo "Test 27: dirt resolved between detection and push -> no_op, no empty stash"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-race.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
printf 'original tracked content\ncontaminating edit\n' > "$REPO/tracked_source.py"

SHIMDIR=$(mktemp -d)
make_git_shim "$SHIMDIR" 2          # 1st --porcelain = detection, 2nd = re-derivation
cat > "$SHIMDIR/inject.sh" <<INJECT
#!/usr/bin/env bash
# The race: something else restores the contaminated file just before the push.
printf 'original tracked content\n' > "$REPO/tracked_source.py"
INJECT
chmod +x "$SHIMDIR/inject.sh"

out=$( cd "$REPO" && PATH="$SHIMDIR:$PATH" "$SCRIPT" --baseline "$SNAP" --quarantine \
        --label "run=RACE issue=5185" 2>&1 ); RC=$?
STASH_LIST=$(git -C "$REPO" stash list)
if [[ "$RC" -eq 0 ]] \
   && [[ "$out" == *'"result":"no_op"'* ]] \
   && [[ "$out" != *'"result":"quarantined"'* ]] \
   && [[ -z "$STASH_LIST" ]]; then
    pass "no stash created and a distinct no_op event logged when the dirt vanishes"
else
    fail "expected rc=0 + no_op event + empty stash list, got rc=$RC; stash='$STASH_LIST'; out=$out"
fi
rm -rf "$SHIMDIR"
rm -rf "$REPO"

echo "Test 28: partial race quarantines only the paths still dirty at push time"
REPO=$(make_repo_with_source)
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-race2.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
printf 'original tracked content\ncontaminating edit\n' > "$REPO/tracked_source.py"
printf 'def leaked():\n    return 42\n' > "$REPO/leaked_module.py"

SHIMDIR=$(mktemp -d)
make_git_shim "$SHIMDIR" 2
cat > "$SHIMDIR/inject.sh" <<INJECT
#!/usr/bin/env bash
# Only ONE of the two offending paths is resolved concurrently.
printf 'original tracked content\n' > "$REPO/tracked_source.py"
INJECT
chmod +x "$SHIMDIR/inject.sh"

out=$( cd "$REPO" && PATH="$SHIMDIR:$PATH" "$SCRIPT" --baseline "$SNAP" --quarantine \
        --label "run=RACE2 issue=5185" 2>&1 ); RC=$?
json_line=$(printf '%s\n' "$out" | grep '"event":"main-clean.quarantine"' | head -1)
STASH_FILES=$(git -C "$REPO" stash show --name-only --include-untracked 'stash@{0}' 2>/dev/null || true)
if [[ "$RC" -eq 4 ]] \
   && [[ "$json_line" == *'"result":"quarantined"'* ]] \
   && [[ "$json_line" == *"leaked_module.py"* ]] \
   && [[ "$json_line" != *"tracked_source.py"* ]] \
   && [[ "$STASH_FILES" == *"leaked_module.py"* ]]; then
    pass "stale path dropped from the pathspec; only the live contamination is rescued"
else
    fail "expected exit 4 naming only leaked_module.py, got rc=$RC; line=$json_line; files=$STASH_FILES"
fi
rm -rf "$SHIMDIR"
rm -rf "$REPO"

echo "Test 29: a push that creates no new entry is not reported as a quarantine"
REPO=$(make_repo_with_source)
# Pre-existing UNRELATED stash entry: the pre-#5185 code read refs/stash after
# the push and would have reported THIS sha as its rescue ref.
printf 'someone elses wip\n' > "$REPO/other_wip.txt"
git -C "$REPO" stash push --include-untracked -q -m "unrelated human wip" -- other_wip.txt
PRE_STASH_SHA=$(git -C "$REPO" rev-parse refs/stash)

SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-race3.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
printf 'original tracked content\ncontaminating edit\n' > "$REPO/tracked_source.py"

# Shim whose `stash push` silently resolves the dirt WITHOUT creating an entry
# (git's own "No local changes to save" no-op shape: exit 0, nothing pushed).
SHIMDIR=$(mktemp -d)
REAL_GIT=$(command -v git)
cat > "$SHIMDIR/git" <<SHIM
#!/usr/bin/env bash
REAL="$REAL_GIT"
for a in "\$@"; do
    if [[ "\$a" == "push" ]]; then
        for b in "\$@"; do
            if [[ "\$b" == "stash" ]]; then
                printf 'original tracked content\n' > "$REPO/tracked_source.py"
                echo "No local changes to save"
                exit 0
            fi
        done
        break
    fi
done
exec "\$REAL" "\$@"
SHIM
chmod +x "$SHIMDIR/git"

out=$( cd "$REPO" && PATH="$SHIMDIR:$PATH" "$SCRIPT" --baseline "$SNAP" --quarantine \
        --label "run=RACE3 issue=5185" 2>&1 ); RC=$?
json_line=$(printf '%s\n' "$out" | grep '"event":"main-clean.quarantine"' | head -1)
if [[ "$RC" -eq 0 ]] \
   && [[ "$json_line" == *'"result":"no_op"'* ]] \
   && [[ "$json_line" != *"$PRE_STASH_SHA"* ]] \
   && [[ "$(git -C "$REPO" rev-parse refs/stash)" == "$PRE_STASH_SHA" ]]; then
    pass "no_op result, and the unrelated pre-existing stash is not claimed as the rescue ref"
else
    fail "expected rc=0 + no_op not naming $PRE_STASH_SHA, got rc=$RC; line=$json_line"
fi
rm -rf "$SHIMDIR"
rm -rf "$REPO"

# ========================================================================
# --list-quarantined: operator-facing surface for outstanding stashes (#5185)
# ========================================================================
# A quarantine was only ever recorded in the structured log, so rescue stashes
# accumulated unreconciled (29 on one host, oldest 7 days) and were noticed by
# accident. This mode is the surface that makes them discoverable.

echo "Test 30: --list-quarantined reports nothing when the stack is clean"
REPO=$(make_repo_with_source)
out=$( cd "$REPO" && "$SCRIPT" --list-quarantined 2>&1 ); RC=$?
outj=$( cd "$REPO" && "$SCRIPT" --list-quarantined --json 2>&1 ); RCJ=$?
if [[ "$RC" -eq 0 && "$RCJ" -eq 0 ]] \
   && [[ "$out" == *"no outstanding"* ]] \
   && [[ "$outj" == *'"count":0'* ]] \
   && [[ "$outj" == *'"stashes":[]'* ]]; then
    pass "exit 0 with an explicit empty report (human + json)"
else
    fail "expected empty reports, got rc=$RC/$RCJ; out=$out; json=$outj"
fi

echo "Test 31: --list-quarantined surfaces a quarantine stash with its attribution"
SNAP="$REPO/.loom/sweep-checkpoint/main-clean-baseline-list.txt"
( cd "$REPO" && "$SCRIPT" --snapshot "$SNAP" >/dev/null 2>&1 )
printf 'def leaked():\n    return 42\n' > "$REPO/leaked_module.py"
( cd "$REPO" && "$SCRIPT" --baseline "$SNAP" --quarantine \
    --label "run=sweep-LISTTEST issue=5185" >/dev/null 2>&1 ); RC=$?
if [[ "$RC" -ne 4 ]]; then fail "Test 31 setup: expected exit 4 from --quarantine, got $RC"; fi
QSHA=$(git -C "$REPO" rev-parse refs/stash)
out=$( cd "$REPO" && "$SCRIPT" --list-quarantined 2>&1 ); RC=$?
outj=$( cd "$REPO" && "$SCRIPT" --list-quarantined --json 2>&1 )
if [[ "$RC" -eq 0 ]] \
   && [[ "$out" == *"1 outstanding"* ]] \
   && [[ "$out" == *"${QSHA:0:12}"* ]] \
   && [[ "$outj" == *'"count":1'* ]] \
   && [[ "$outj" == *'"producer":"quarantine"'* ]] \
   && [[ "$outj" == *'"issue":"5185"'* ]] \
   && [[ "$outj" == *'"run":"sweep-LISTTEST"'* ]] \
   && [[ "$outj" == *'"empty":false'* ]]; then
    pass "outstanding quarantine listed with commit, issue and run attribution"
else
    fail "expected the quarantine listed with attribution, got rc=$RC; out=$out; json=$outj"
fi

echo "Test 32: --list-quarantined covers other Loom producers, ignores human stashes"
printf 'drift\n' > "$REPO/drift.txt"
git -C "$REPO" stash push --include-untracked -q -m "auditor-tmp-drift-stash-1785796450" -- drift.txt
printf 'my own wip\n' > "$REPO/human_wip.txt"
git -C "$REPO" stash push --include-untracked -q -m "human scratch, not Loom's" -- human_wip.txt
outj=$( cd "$REPO" && "$SCRIPT" --list-quarantined --json 2>&1 ); RC=$?
if [[ "$RC" -eq 0 ]] \
   && [[ "$outj" == *'"count":2'* ]] \
   && [[ "$outj" == *'"producer":"auditor-drift"'* ]] \
   && [[ "$outj" != *"human scratch"* ]]; then
    pass "auditor drift stash included, human stash excluded"
else
    fail "expected count 2 including auditor-drift and excluding the human stash, got rc=$RC; json=$outj"
fi
rm -rf "$REPO"

echo "Test 33: --list-quarantined flags an EMPTY entry"
REPO=$(make_repo_with_source)
# Reproduce the entries observed in the wild (three of five quarantine stashes
# in one consumer repo held nothing at all). Built with plumbing because modern
# git refuses to create an empty stash — the same stash-commit shape (working
# tree + index parents, tree identical to HEAD's) with nothing recorded.
EMPTY_MSG="On master: loom-quarantine: run=EMPTY issue=1"
HEAD_SHA=$(git -C "$REPO" rev-parse HEAD)
HEAD_TREE=$(git -C "$REPO" rev-parse 'HEAD^{tree}')
INDEX_COMMIT=$(git -C "$REPO" commit-tree -p "$HEAD_SHA" -m "index on master" "$HEAD_TREE")
EMPTY_STASH=$(git -C "$REPO" commit-tree -p "$HEAD_SHA" -p "$INDEX_COMMIT" -m "$EMPTY_MSG" "$HEAD_TREE")
git -C "$REPO" update-ref --create-reflog refs/stash "$EMPTY_STASH" -m "$EMPTY_MSG"
out=$( cd "$REPO" && "$SCRIPT" --list-quarantined 2>&1 ); RC=$?
outj=$( cd "$REPO" && "$SCRIPT" --list-quarantined --json 2>&1 )
if [[ "$RC" -eq 0 ]] \
   && [[ "$out" == *"EMPTY"* ]] \
   && [[ "$outj" == *'"empty":true'* ]] \
   && [[ "$outj" == *'"empty_count":1'* ]]; then
    pass "an entry that captured nothing is flagged EMPTY in both outputs"
else
    fail "expected an EMPTY flag, got rc=$RC; out=$out; json=$outj"
fi
rm -rf "$REPO"

echo "Test 34: --list-quarantined rejects being combined with a check mode"
REPO=$(make_repo_with_source)
( cd "$REPO" && "$SCRIPT" --list-quarantined --quarantine >/dev/null 2>&1 ); RC1=$?
( cd "$REPO" && "$SCRIPT" --list-quarantined --baseline "$REPO/nope.txt" >/dev/null 2>&1 ); RC2=$?
( cd "$REPO" && "$SCRIPT" --json >/dev/null 2>&1 ); RC3=$?
if [[ "$RC1" -eq 2 && "$RC2" -eq 2 && "$RC3" -eq 2 ]]; then
    pass "exit 2 for --list-quarantined + check mode, and for a bare --json"
else
    fail "expected 2/2/2, got quarantine=$RC1 baseline=$RC2 json=$RC3"
fi
rm -rf "$REPO"

# -------- Summary --------
echo ""
if [[ "$TESTS_FAILED" -eq 0 ]]; then
    echo -e "${GREEN}All $TESTS_PASSED/$TESTS_RUN tests passed${NC}"
    exit 0
else
    echo -e "${RED}FAILED: $TESTS_FAILED/$TESTS_RUN tests failed${NC}"
    exit 1
fi
