#!/usr/bin/env bash
# test-filing-lock.sh — Tests for the machine-wide issue-filing lock (#6714)
#
# Covers defaults/scripts/lib/filing-lock.sh, the Bash half of the cross-process
# lock that replaced #3707's documentation-only "do not run concurrent
# Architects" mitigation after it regressed on 2026-08-08 and silently corrupted
# five issue bodies across two repos for 13 days:
#
#   1. acquire/release round-trip — the holder dir + owner.json appear/disappear.
#   2. THE #6714 REGRESSION — two concurrent issue-creating agents in DIFFERENT
#      repos, asserting each filed body matches the request that produced it.
#   3. BOUNDED + FAIL-SAFE — a contended acquire returns 75 (DEFER) rather than
#      filing unserialized. This is the inverse of build-slot.sh's degrade-open.
#   4. crashed holder — a dead owner PID on this host is reaped immediately.
#   5. crashed holder — an aged holder with no owner record is reaped by mtime.
#   6. a foreign host's PID is never used for liveness.
#   7. degrade OPEN when the lock store is unusable (a FILE in the way).
#   8. LOOM_FILING_LOCK=0 disables serialization entirely.
#   9. re-entrancy — LOOM_FILING_LOCK_HELD makes a nested acquire a no-op.
#  10. fleet tier — a live peer hold blocks filing and expires on its TTL.
#  11. create-issue.sh actually takes the lock (the wiring, not just the lib).
#
# Style follows test-build-slot.sh: throwaway mktemp dirs, assert harness, no
# network, no real ~/.loom.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FILING_LOCK_LIB="$SCRIPTS_DIR/lib/filing-lock.sh"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib/assert.sh"

assert_eq() {
    if [[ "$1" == "$2" ]]; then pass "$3"; else fail "$3 (expected '$2', got '$1')"; fi
}
assert_true() { if [[ "$1" == "0" ]]; then pass "$2"; else fail "$2 (expected success, got rc=$1)"; fi; }

# NOTE on the `( … ) && CASE_RC=0 || CASE_RC=$?` idiom: every case runs in a
# subshell so its env exports cannot leak into the next one, and the rc is
# captured explicitly so a FAILING case reports as a test failure instead of
# aborting the whole suite under `set -e`.

# shellcheck source=../lib/filing-lock.sh
source "$FILING_LOCK_LIB"

TMPROOT="$(mktemp -d)"
cleanup() { rm -rf "$TMPROOT"; }
trap cleanup EXIT

new_store() {
    local d
    d="$(mktemp -d "$TMPROOT/store.XXXXXX")"
    printf '%s\n' "$d/issue-filing"
}

# Fast bounds so the suite stays quick.
export LOOM_FILING_LOCK_WAIT_SECS=1
# Never reach the real safehouse socket from a test.
unset SAFEHOUSED_SOCKET LOOM_SAFEHOUSE_SOCKET SAFEHOUSE_PERSONA || true
unset LOOM_FILING_LOCK_HELD || true

echo ""
echo "=== filing-lock.sh: acquire / release round-trip ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    loom_filing_lock_acquire "architect" >/dev/null 2>&1
    [[ -d "$DIR/holder" ]] || exit 1
    [[ -f "$DIR/holder/owner.json" ]] || exit 1
    grep -q '"label":"architect"' "$DIR/holder/owner.json" || exit 1
    [[ "$LOOM_FILING_LOCK_PATH" == "$DIR/holder" ]] || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "acquire creates \$store/holder + owner.json; release removes both"

echo ""
echo "=== THE #6714 REGRESSION: concurrent filers in DIFFERENT repos ==="

# Two issue-creating agents, each filing a 4-issue burst into a *different*
# repo, sharing one scratch body path — the only way two processes can swap each
# other's body text, and exactly the 2026-08-08 shape (gf180-sram's bodies
# overwritten with sky130-modexp's). Each filer writes its body, pauses, reads
# it back and "files" it. Without the lock the read-back is the other repo's
# body; with it, every filed body matches the request that produced it.
DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=30
    SCRATCH="$DIR/scratch-body.md"
    FILED="$DIR/filed.txt"
    TAB=$'\t'
    mkdir -p "$DIR"
    : > "$FILED"

    filer() { # <repo> <body>
        local repo="$1" body="$2" i
        for i in 1 2 3 4; do
            loom_filing_lock_acquire "$repo" >/dev/null 2>&1 || exit 75
            printf '%s #%s' "$body" "$i" > "$SCRATCH"
            sleep 0.05
            printf '%s%s%s\n' "$repo" "$TAB" "$(cat "$SCRATCH")" >> "$FILED"
            loom_filing_lock_release >/dev/null 2>&1
        done
    }

    ( filer "gf180-sram" "SRAM macro: 512x8 bitcell array" ) &
    P1=$!
    ( filer "sky130-modexp" "RTL synthesis: rtl/modexp.v timing closure" ) &
    P2=$!
    wait "$P1" || exit 1
    wait "$P2" || exit 1

    [[ "$(wc -l < "$FILED")" -eq 8 ]] || exit 1
    # Every filed body must carry ITS OWN repo's text: all 8 rows must match
    # the correct (repo, body-prefix) pairing, so any single crossed body fails.
    [[ "$(grep -c "^gf180-sram${TAB}SRAM macro" "$FILED")" -eq 4 ]] || exit 1
    [[ "$(grep -c "^sky130-modexp${TAB}RTL synthesis" "$FILED")" -eq 4 ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" \
    "two concurrent filers in DIFFERENT repos each file their own body (the #6714 regression)"

# The unserialized control, forced deterministically: without the lock, filer A
# reads back filer B's body. Proves the case above is exercising the mechanism.
DIR="$(new_store)"
(
    mkdir -p "$DIR"
    SCRATCH="$DIR/scratch-body.md"
    printf 'SRAM macro: 512x8 bitcell array' > "$SCRATCH"   # A writes
    printf 'RTL synthesis: rtl/modexp.v' > "$SCRATCH"       # B overwrites
    A_FILED="$(cat "$SCRATCH")"                             # A reads back
    [[ "$A_FILED" == RTL* ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" \
    "control: unserialized, the same interleaving DOES cross-contaminate"

echo ""
echo "=== bounded wait is FAIL-SAFE (defer), not fail-open ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1
    # A live holder owned by THIS shell's pid, so no reap leg can fire.
    mkdir -p "$DIR/holder"
    printf '{"host":"%s","pid":%s,"label":"burst","acquired_at":%s}\n' \
        "$(loom_filing_lock_host)" "$$" "$(date +%s)" > "$DIR/holder/owner.json"
    RC=0
    loom_filing_lock_acquire "second-filer" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 75 ]] || exit 1
    # And nothing was taken.
    [[ -z "${LOOM_FILING_LOCK_PATH:-}" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a contended acquire returns 75 (DEFER), never 0-without-a-lock"

echo ""
echo "=== a crashed holder cannot wedge issue creation ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    mkdir -p "$DIR/holder"
    # A killed holder: the lock survives, the process does not. Spawn a real
    # child and reap it so its PID is genuinely dead (never a recycled guess).
    ( exit 0 ) &
    DEAD_PID=$!
    wait "$DEAD_PID" 2>/dev/null || true
    printf '{"host":"%s","pid":%s,"label":"crashed-architect","acquired_at":%s}\n' \
        "$(loom_filing_lock_host)" "$DEAD_PID" "$(date +%s)" > "$DIR/holder/owner.json"
    loom_filing_lock_acquire "next-architect" >/dev/null 2>&1 || exit 1
    grep -q '"label":"next-architect"' "$DIR/holder/owner.json" || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a dead owner PID on THIS host is reaped immediately"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_STALE_SECS=1
    mkdir -p "$DIR/holder"   # no owner.json — only the mtime leg can free it
    sleep 1.1
    loom_filing_lock_acquire "next" >/dev/null 2>&1 || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an aged holder with NO owner record is reaped by mtime"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1
    mkdir -p "$DIR/holder"
    ( exit 0 ) &
    DEAD_PID=$!
    wait "$DEAD_PID" 2>/dev/null || true
    # Same dead PID, but recorded against ANOTHER host: a PID number from
    # another machine says nothing about a process here, so the dead-PID leg
    # must NOT fire and the acquire must defer.
    printf '{"host":"some-other-host","pid":%s,"label":"remote","acquired_at":%s}\n' \
        "$DEAD_PID" "$(date +%s)" > "$DIR/holder/owner.json"
    RC=0
    loom_filing_lock_acquire "local" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 75 ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a foreign host's PID is never used for liveness"

echo ""
echo "=== degrade open only when there is no lock to take ==="

DIR="$(new_store)"
(
    mkdir -p "$(dirname "$DIR")"
    printf 'x' > "$DIR"           # a FILE where the store must be
    export LOOM_FILING_LOCK_DIR="$DIR"
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || exit 1
    [[ -z "${LOOM_FILING_LOCK_PATH:-}" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an unusable store degrades OPEN (rc 0, no lock), never defers"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK=0
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || exit 1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_FILING_LOCK=0 disables serialization entirely"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_HELD=1
    loom_filing_lock_acquire "nested" >/dev/null 2>&1 || exit 1
    [[ ! -e "$DIR/holder" ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "LOOM_FILING_LOCK_HELD makes a nested acquire a re-entrant no-op"

echo ""
echo "=== fleet tier: a mirrored peer hold blocks, then expires ==="

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_WAIT_SECS=1
    export LOOM_FILING_LOCK_PEER_TTL_SECS=3600
    # What safehouse::PeerClaimSink writes on an observed peer FilingLock ad.
    mkdir -p "$DIR/peers"
    printf 'host-remote' > "$DIR/peers/host-remote"
    RC=0
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || RC=$?
    [[ "$RC" -eq 75 ]] || exit 1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "a live peer hold defers local filing (the cross-host tier)"

DIR="$(new_store)"
(
    export LOOM_FILING_LOCK_DIR="$DIR"
    export LOOM_FILING_LOCK_PEER_TTL_SECS=1
    mkdir -p "$DIR/peers"
    printf 'host-remote' > "$DIR/peers/host-remote"
    sleep 1.1
    # A peer that crashed without sending FilingUnlock must not wedge us.
    loom_filing_lock_acquire "architect" >/dev/null 2>&1 || exit 1
    [[ ! -e "$DIR/peers/host-remote" ]] || exit 1
    loom_filing_lock_release >/dev/null 2>&1
    exit 0
) && CASE_RC=0 || CASE_RC=$?
assert_true "$CASE_RC" "an expired peer hold is pruned and never wedges filing"

echo ""
echo "=== store resolution ==="

assert_eq "$(LOOM_FILING_LOCK_DIR="/tmp/x" loom_filing_lock_store)" "/tmp/x" \
    "LOOM_FILING_LOCK_DIR overrides the default location"
assert_eq "$(LOOM_FILING_LOCK_DIR="   " HOME=/h loom_filing_lock_store)" "/h/.loom/locks/issue-filing" \
    "a blank override falls back to ~/.loom/locks/issue-filing"

echo ""
echo "=== create-issue.sh is wired to the lock ==="

if grep -q "lib/filing-lock.sh" "$SCRIPTS_DIR/create-issue.sh"; then
    pass "create-issue.sh sources lib/filing-lock.sh"
else
    fail "create-issue.sh no longer takes the filing lock — the #6714 serialization point is gone"
fi
if grep -q "loom_filing_lock_acquire" "$SCRIPTS_DIR/create-issue.sh" \
    && grep -q "LOOM_FILING_LOCK_DEFER_RC" "$SCRIPTS_DIR/create-issue.sh"; then
    pass "create-issue.sh acquires the lock and propagates the DEFER exit code"
else
    fail "create-issue.sh does not acquire the lock / does not propagate the DEFER exit code"
fi

echo ""
echo "=== host identity comes from loom-daemon host-id (#10023) ==="

HID_DIR="$(mktemp -d)"
printf '#!/usr/bin/env bash\n[[ "${1:-}" == "host-id" ]] && { echo persisted-host-abc; exit 0; }\nexit 0\n' > "$HID_DIR/fake-daemon"
printf '#!/usr/bin/env bash\nexit 1\n' > "$HID_DIR/failing-daemon"
chmod +x "$HID_DIR/fake-daemon" "$HID_DIR/failing-daemon"
GOT="$(env -u LOOM_HOST_ID HOSTNAME=some-distinct-hostname LOOM_DAEMON_BIN="$HID_DIR/fake-daemon" \
    bash -c 'source "$1"; loom_filing_lock_host' _ "$FILING_LOCK_LIB")"
[[ "$GOT" == "persisted-host-abc" ]] && pass "owner host equals loom-daemon host-id, not \$HOSTNAME" \
    || fail "owner host was '$GOT', expected persisted-host-abc"
GOT="$(env -u LOOM_HOST_ID HOSTNAME=some-distinct-hostname LOOM_DAEMON_BIN="$HID_DIR/failing-daemon" \
    bash -c 'source "$1"; loom_filing_lock_host' _ "$FILING_LOCK_LIB")"
[[ "$GOT" == "unknown-host" ]] && pass "a failing host-id yields the unknown-host literal" \
    || fail "failing host-id produced '$GOT', expected unknown-host"
rm -rf "$HID_DIR"

echo ""
echo "==============================================="
echo "  Tests run:    $TESTS_RUN"
echo -e "  ${GREEN}Passed:${NC}       $TESTS_PASSED"
echo -e "  ${RED}Failed:${NC}       $TESTS_FAILED"
echo "==============================================="

[[ "$TESTS_FAILED" -eq 0 ]]
