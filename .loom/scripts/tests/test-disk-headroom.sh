#!/usr/bin/env bash
# test-disk-headroom.sh — Tests for the resource-gated wave-size helper (#3566)
#
# Covers defaults/scripts/lib/disk-headroom.sh:
#
#   1. loom_wave_size_from_disk — pure integer clamping across the whole matrix
#      (daemon vs subagent target, disk-bound, target-bound, candidate-bound,
#      floor-of-1), plus the reason token and env-tunable PER_WORKTREE_GB.
#   2. loom_worktree_root_free_gb — GB conversion with a stubbed df on PATH.
#   3. loom_worktree_root_free_gb — with LOOM_WORKTREE_ROOT pointed at a scratch
#      tmpdir, proves the helper df's the RESOLVED worktree root (the scratch
#      volume), not the repo drive. Regression guard for the core #3566 AC.
#
# Pattern follows test-worktree-root-override.sh: throwaway dirs in mktemp, a
# df stub on PATH, assert-style harness.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

DISK_HEADROOM_LIB="$SCRIPTS_DIR/lib/disk-headroom.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TESTS_RUN=0
TESTS_PASSED=0
TESTS_FAILED=0

pass() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_PASSED=$((TESTS_PASSED + 1)); echo -e "  ${GREEN}PASS${NC}: $1"; }
fail() { TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAILED=$((TESTS_FAILED + 1)); echo -e "  ${RED}FAIL${NC}: $1"; }

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}

# shellcheck source=../lib/disk-headroom.sh
source "$DISK_HEADROOM_LIB"

# Convenience: run loom_wave_size_from_disk and capture "size|reason".
wave() {
    local out size reason
    out="$(loom_wave_size_from_disk "$@")"
    size="$(printf '%s\n' "$out" | sed -n '1p')"
    reason="$(printf '%s\n' "$out" | sed -n '2p')"
    echo "${size}|${reason}"
}

# --- Test 1: pure wave-size math (daemon target = 10) ---
echo "Test 1: loom_wave_size_from_disk daemon path (target 10)"

# Plentiful disk, plenty of candidates -> target-bound at 10.
assert_eq "$(wave daemon 20 100)" "10|target" "daemon: plentiful disk + candidates -> 10 (target)"

# Candidate-bound: only 3 issues, disk and target both higher.
assert_eq "$(wave daemon 3 100)" "3|candidates" "daemon: 3 candidates clamps to 3 (candidates)"

# Disk-bound: free=6 GB, per=2 -> max_by_disk=3, below target and candidates.
assert_eq "$(wave daemon 10 6)" "3|disk" "daemon: 6GB free (per 2) clamps to 3 (disk)"

# Floor-of-1: nearly full disk (free=1, per=2 -> 0) never returns 0.
assert_eq "$(wave daemon 10 1)" "1|floor" "daemon: 1GB free floors to 1 (floor)"

# --- Test 2: pure wave-size math (subagent path, explicit cap) ---
echo ""
echo "Test 2: loom_wave_size_from_disk subagent path (explicit LOOM_SUBAGENT_WAVE_CAP=3)"

# These cases pin the explicit-cap behaviour of loom_wave_size_from_disk (its
# signature and internals are UNCHANGED by #3693 — the core-scaling happens in
# the caller, which resolves LOOM_SUBAGENT_WAVE_CAP before invoking). We set the
# cap explicitly here so the assertions are deterministic regardless of the
# host's actual core count.
export LOOM_SUBAGENT_WAVE_CAP=3

# Plentiful disk + candidates -> capped at 3 (the #3289-safe target).
assert_eq "$(wave subagent 20 100)" "3|target" "subagent: plentiful -> 3 (explicit cap, NOT 10)"

# Candidate-bound: 2 issues -> 2.
assert_eq "$(wave subagent 2 100)" "2|candidates" "subagent: 2 candidates clamps to 2"

# Disk-bound below the cap: free=2, per=2 -> 1.
assert_eq "$(wave subagent 10 2)" "1|disk" "subagent: 2GB free (per 2) clamps to 1 (disk)"

# Floor: free=0 -> 1.
assert_eq "$(wave subagent 10 0)" "1|floor" "subagent: 0GB free floors to 1"

# A higher explicit cap is honoured verbatim (proves the env value wins).
assert_eq "$(LOOM_SUBAGENT_WAVE_CAP=6 wave subagent 20 100)" "6|target" "subagent: explicit cap 6 honoured -> 6 (target)"

unset LOOM_SUBAGENT_WAVE_CAP

# --- Test 3: PER_WORKTREE_GB env override ---
echo ""
echo "Test 3: LOOM_PER_WORKTREE_GB env override changes the disk clamp"

# With per=5, free=100 -> max_by_disk=20 -> daemon target 10 still wins.
assert_eq "$(LOOM_PER_WORKTREE_GB=5 wave daemon 15 100)" "10|target" "per=5, 100GB -> 10 (target)"
# With per=25, free=100 -> max_by_disk=4 -> disk-bound at 4.
assert_eq "$(LOOM_PER_WORKTREE_GB=25 wave daemon 15 100)" "4|disk" "per=25, 100GB -> 4 (disk)"

# --- Test 4: unknown mechanism is rejected ---
echo ""
echo "Test 4: unknown mechanism errors (non-zero exit)"
if loom_wave_size_from_disk bogus 5 100 >/dev/null 2>&1; then
    fail "unknown mechanism should return non-zero"
else
    pass "unknown mechanism returns non-zero"
fi

# --- Test 5: loom_worktree_root_free_gb GB conversion via stubbed df ---
echo ""
echo "Test 5: loom_worktree_root_free_gb converts df 1K blocks to GB"
STUBDIR=$(mktemp -d /tmp/loom-dh-stub.XXXXXX)
ARGLOG="$STUBDIR/df-args.log"
# Stub df: record the path argument, emit a POSIX -Pk table with a fixed
# Available column. 20971520 1K-blocks = 20 GB.
cat > "$STUBDIR/df" <<EOF
#!/usr/bin/env bash
# Record the last (path) argument for the regression assertion.
for a in "\$@"; do :; done
echo "\$a" >> "$ARGLOG"
echo "Filesystem     1024-blocks      Used Available Capacity Mounted on"
echo "/dev/stub         52428800  31457280  20971520      60% /stub"
EOF
chmod +x "$STUBDIR/df"

REPO=$(mktemp -d /tmp/loom-dh-repo.XXXXXX)
# No override: worktree root resolves to $REPO/.loom/worktrees (walks up to $REPO).
gb=$(PATH="$STUBDIR:$PATH" loom_worktree_root_free_gb "$REPO")
assert_eq "$gb" "20" "df 20971520 1K-blocks converts to 20 GB"

# --- Test 6: measures the RESOLVED worktree root (scratch volume) ---
echo ""
echo "Test 6: LOOM_WORKTREE_ROOT override -> df targets the scratch volume, not the repo drive"
: > "$ARGLOG"
SCRATCH=$(mktemp -d /tmp/loom-dh-scratch.XXXXXX)
# Materialize the namespaced leaf so df receives the exact worktree root.
mkdir -p "$SCRATCH/$(basename "$REPO")"
gb=$(PATH="$STUBDIR:$PATH" LOOM_WORKTREE_ROOT="$SCRATCH" loom_worktree_root_free_gb "$REPO")
assert_eq "$gb" "20" "override path still converts df output to 20 GB"

DF_PATH=$(tail -n 1 "$ARGLOG")
assert_eq "$DF_PATH" "$SCRATCH/$(basename "$REPO")" "df measured the resolved scratch worktree root"
# Regression guard: it must NOT have measured the repo drive.
if [[ "$DF_PATH" == "$REPO"* ]]; then
    fail "df measured the repo drive ($DF_PATH) instead of the scratch volume"
else
    pass "df did not measure the repo drive (scratch volume used)"
fi

rm -rf "$STUBDIR" "$REPO" "$SCRATCH"

# --- Test 7: disk-headroom.sh is sourceable directly under zsh (#3680) ---
echo ""
echo "Test 7: disk-headroom.sh sources cleanly under zsh (regression guard for #3680)"
# The original bug: sweep.md's Stage -1 does `source ./.loom/scripts/lib/disk-headroom.sh`
# directly into the invoking shell, which on macOS is frequently zsh. Under zsh
# BASH_SOURCE is unset, so the bare `${BASH_SOURCE[0]}` resolved the sibling
# worktree-root.sh against the CWD (repo root) instead of the lib dir, and the
# source failed with "no such file or directory: .../worktree-root.sh". The fix
# is the portable `${BASH_SOURCE[0]:-$0}` idiom. This test is itself executed
# under bash (the harness shebang), so it must shell out to zsh explicitly to
# exercise the reported path — that is exactly why the pre-existing tests above
# (all run under bash) never caught the bug.
if command -v zsh >/dev/null 2>&1; then
    # Mirror sweep.md's invocation shape: cd into the dir that holds `lib/` and
    # source via a leading-./ relative path. Here that dir is $SCRIPTS_DIR.
    zsh_out="$(zsh -c "cd '$SCRIPTS_DIR' && source ./lib/disk-headroom.sh && echo OK" 2>&1)" || true
    if [[ "$zsh_out" == *OK* ]]; then
        pass "disk-headroom.sh sources cleanly under zsh (relative ./lib/ path)"
    else
        fail "disk-headroom.sh failed to source under zsh: $zsh_out"
    fi

    # Also exercise the functions post-source under zsh — proves the sibling
    # worktree-root.sh loaded (loom_worktree_root_free_gb calls loom_worktree_root
    # from it) and the pure math function works in a zsh interpreter.
    zsh_math="$(zsh -c "cd '$SCRIPTS_DIR' && source ./lib/disk-headroom.sh && loom_wave_size_from_disk daemon 20 100 | tr '\n' '|'" 2>&1)" || true
    assert_eq "$zsh_math" "10|target|" "loom_wave_size_from_disk works under zsh after sourcing"
else
    echo "  SKIP: zsh not available on PATH — skipping zsh-sourcing regression test"
fi

# --- Test 8: loom_subagent_target_from_cores pure clamp (#3693) ---
echo ""
echo "Test 8: loom_subagent_target_from_cores = clamp(floor((cores-2)/4), 3, 6)"

# Floor of 3 holds for small/shared hosts (raw < 3, incl. cores < 2).
assert_eq "$(loom_subagent_target_from_cores 1)" "3" "cores=1 -> 3 (floor; raw would be <=0)"
assert_eq "$(loom_subagent_target_from_cores 4)" "3" "cores=4 -> 3 (floor)"
assert_eq "$(loom_subagent_target_from_cores 14)" "3" "cores=14 -> 3 (floor; raw=3)"
assert_eq "$(loom_subagent_target_from_cores 16)" "3" "cores=16 -> 3 (floor; raw=3)"
# Scaling band 3..6.
assert_eq "$(loom_subagent_target_from_cores 20)" "4" "cores=20 -> 4 (raw=4)"
assert_eq "$(loom_subagent_target_from_cores 22)" "5" "cores=22 -> 5 (raw=5)"
assert_eq "$(loom_subagent_target_from_cores 24)" "5" "cores=24 -> 5 (raw=5)"
assert_eq "$(loom_subagent_target_from_cores 28)" "6" "cores=28 -> 6 (raw=6, ceiling)"
# Ceiling of 6 holds for very large hosts.
assert_eq "$(loom_subagent_target_from_cores 64)" "6" "cores=64 -> 6 (ceiling)"
assert_eq "$(loom_subagent_target_from_cores 100)" "6" "cores=100 -> 6 (ceiling)"

# Non-integer input is rejected with a non-zero exit.
if loom_subagent_target_from_cores "abc" >/dev/null 2>&1; then
    fail "non-integer cores should return non-zero"
else
    pass "non-integer cores returns non-zero"
fi

# --- Test 9: loom_detect_cores portability + override (#3693) ---
echo ""
echo "Test 9: loom_detect_cores honors LOOM_CORES_OVERRIDE and returns a positive integer"

# Deterministic path: override short-circuits all host probing.
assert_eq "$(LOOM_CORES_OVERRIDE=7 loom_detect_cores)" "7" "LOOM_CORES_OVERRIDE=7 -> 7 (deterministic)"
assert_eq "$(LOOM_CORES_OVERRIDE=28 loom_detect_cores)" "28" "LOOM_CORES_OVERRIDE=28 -> 28 (deterministic)"

# Host-dependent smoke check: no override -> a positive integer.
detected="$(loom_detect_cores)"
if [[ "$detected" =~ ^[0-9]+$ && "$detected" -ge 1 ]]; then
    pass "loom_detect_cores (no override) returns a positive integer ($detected)"
else
    fail "loom_detect_cores (no override) returned non-positive-integer '$detected'"
fi

# End-to-end: the documented composition prints the core-scaled default.
assert_eq "$(LOOM_CORES_OVERRIDE=28 bash -c "source '$DISK_HEADROOM_LIB'; loom_subagent_target_from_cores \"\$(loom_detect_cores)\"")" "6" \
    "composition: LOOM_CORES_OVERRIDE=28 -> target 6"

# --- Summary ---
echo ""
echo "Tests run: $TESTS_RUN, Passed: $TESTS_PASSED, Failed: $TESTS_FAILED"
[[ $TESTS_FAILED -eq 0 ]] || exit 1
