#!/bin/bash
# test-sweep-run-registry.sh - Smoke tests for the sweep run-identity registry (#3768).
#
# Exercises `new` (stable run id generation + registration), `peers` (live-peer
# listing + dead-PID pruning), `cleanup`, and `list`, plus the concurrency
# properties the /sweep skill relies on:
#   - each `new` yields a distinct run id (concurrent sweeps don't collide),
#   - a run never lists itself as a peer,
#   - a dead-PID entry is pruned and never warns forever,
#   - the run id is filename/JSON-safe.
#
# Run from anywhere — uses an isolated TMPDIR so it never touches a real
# workspace's .loom/sweep-run/.

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER="$SCRIPT_DIR/../sweep-run-registry.sh"

if [[ ! -x "$HELPER" ]]; then
    echo "FAIL: helper not executable at $HELPER" >&2
    exit 1
fi

TMP_REPO="$(mktemp -d)"
# Spawned long-lived helper PIDs we must reap on exit.
LIVE_PIDS=()
cleanup() {
    local p
    for p in "${LIVE_PIDS[@]:-}"; do
        [[ -n "$p" ]] && kill "$p" 2>/dev/null
    done
    rm -rf "$TMP_REPO"
}
trap cleanup EXIT

cd "$TMP_REPO" || exit 1
git init -q .
mkdir -p .loom/scripts
cp "$HELPER" .loom/scripts/sweep-run-registry.sh
chmod +x .loom/scripts/sweep-run-registry.sh
REG="$TMP_REPO/.loom/scripts/sweep-run-registry.sh"

PASS=0
FAIL=0
assert_eq() {
    local desc="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
        echo "PASS: $desc"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (expected '$expected', got '$actual')" >&2
        FAIL=$((FAIL + 1))
    fi
}
assert_exit() {
    local desc="$1" expected="$2"; shift 2
    "$@" >/dev/null 2>&1
    local actual=$?
    if [[ $actual -eq $expected ]]; then
        echo "PASS: $desc (exit $actual)"
        PASS=$((PASS + 1))
    else
        echo "FAIL: $desc (expected exit $expected, got $actual)" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Spawn a durable background process we control the lifetime of; echo its PID.
# Redirect the child's stdio to /dev/null so it does not hold the command-
# substitution pipe open (a backgrounded proc inside $() otherwise blocks until
# its stdout closes). The array append happens in the PARENT after each call —
# a $()-subshell append would be lost — so the EXIT trap can reap every child.
spawn_live() {
    sleep 300 >/dev/null 2>&1 &
    echo "$!"
}

# 1. `new` prints a run id in the documented shape.
LIVE1=$(spawn_live); LIVE_PIDS+=("$LIVE1")
RID1=$("$REG" new --pid "$LIVE1")
if [[ "$RID1" =~ ^sweep-[0-9]{8}T[0-9]{6}Z-[0-9]+-[0-9a-f]{8}$ ]]; then
    echo "PASS: run id matches expected portable shape ($RID1)"
    PASS=$((PASS + 1))
else
    echo "FAIL: run id has unexpected shape: $RID1" >&2
    FAIL=$((FAIL + 1))
fi

# 2. run id is filename/JSON-safe (charset [A-Za-z0-9-]).
if [[ "$RID1" =~ ^[A-Za-z0-9-]+$ ]]; then
    echo "PASS: run id is filename/JSON-safe"
    PASS=$((PASS + 1))
else
    echo "FAIL: run id contains unsafe chars: $RID1" >&2
    FAIL=$((FAIL + 1))
fi

# 3. registration wrote a gitignored registry file.
if [[ -f "$TMP_REPO/.loom/sweep-run/${RID1}.json" ]]; then
    echo "PASS: registry file created for run 1"
    PASS=$((PASS + 1))
else
    echo "FAIL: registry file missing for run 1" >&2
    FAIL=$((FAIL + 1))
fi

# 4. Two `new` calls yield distinct run ids (concurrent sweeps don't collide).
LIVE2=$(spawn_live); LIVE_PIDS+=("$LIVE2")
RID2=$("$REG" new --pid "$LIVE2")
if [[ "$RID1" != "$RID2" ]]; then
    echo "PASS: distinct run ids across two new calls"
    PASS=$((PASS + 1))
else
    echo "FAIL: two new calls produced the same run id: $RID1" >&2
    FAIL=$((FAIL + 1))
fi

# 5. A run never lists itself as a peer.
out=$("$REG" peers "$RID1")
if echo "$out" | grep -q "$RID1"; then
    echo "FAIL: run listed itself as a peer: $out" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: run does not list itself as a peer"
    PASS=$((PASS + 1))
fi

# 6. peers of RID1 report RID2 as a live peer (pid + timestamp columns present).
out=$("$REG" peers "$RID1")
if echo "$out" | grep -q "^$RID2 $LIVE2 "; then
    echo "PASS: live peer reported with pid and timestamp"
    PASS=$((PASS + 1))
else
    echo "FAIL: expected live peer $RID2 (pid $LIVE2), got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 7. Kill peer 2 → it is no longer a live peer AND its entry is pruned.
kill "$LIVE2" 2>/dev/null
wait "$LIVE2" 2>/dev/null
out=$("$REG" peers "$RID1")
assert_eq "no live peers after peer killed" "" "$out"
if [[ -f "$TMP_REPO/.loom/sweep-run/${RID2}.json" ]]; then
    echo "FAIL: dead peer entry not pruned (would warn forever)" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: dead-PID peer entry pruned"
    PASS=$((PASS + 1))
fi

# 8. list shows only the surviving run 1.
out=$("$REG" list)
if echo "$out" | grep -q "^$RID1 " && ! echo "$out" | grep -q "$RID2"; then
    echo "PASS: list shows surviving run only"
    PASS=$((PASS + 1))
else
    echo "FAIL: list unexpected after prune: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 9. cleanup removes the run's own entry.
"$REG" cleanup "$RID1"
if [[ -f "$TMP_REPO/.loom/sweep-run/${RID1}.json" ]]; then
    echo "FAIL: cleanup did not remove own entry" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: cleanup removed own entry"
    PASS=$((PASS + 1))
fi

# 10. peers on an empty/nonexistent registry is empty, exit 0 (single-sweep case).
out=$("$REG" peers "sweep-nonexistent")
assert_eq "peers empty when no registry entries" "" "$out"
assert_exit "peers exits 0 with no entries" 0 "$REG" peers "sweep-nonexistent"

# 11. peers without a RUN_ID arg is a usage error (exit 1).
assert_exit "peers requires RUN_ID arg" 1 "$REG" peers

# 12. new rejects a non-numeric --pid.
assert_exit "new rejects non-numeric --pid" 1 "$REG" new --pid abc

# 13. cleanup of an already-absent entry is a no-op (exit 0).
assert_exit "cleanup of missing entry exits 0" 0 "$REG" cleanup "$RID1"

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
