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
if grep -q "$RID1" <<<"$out"; then
    echo "FAIL: run listed itself as a peer: $out" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: run does not list itself as a peer"
    PASS=$((PASS + 1))
fi

# 6. peers of RID1 report RID2 as a live peer (pid + timestamp columns present).
out=$("$REG" peers "$RID1")
if grep -q "^$RID2 $LIVE2 " <<<"$out"; then
    echo "PASS: live peer reported with pid and timestamp"
    PASS=$((PASS + 1))
else
    echo "FAIL: expected live peer $RID2 (pid $LIVE2), got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 6b. Both runs have a RUN_ID-keyed main-clean baseline (as /loom:sweep writes).
BASELINE_DIR="$TMP_REPO/.loom/sweep-checkpoint"
mkdir -p "$BASELINE_DIR"
: > "$BASELINE_DIR/main-clean-baseline-${RID1}.txt"
: > "$BASELINE_DIR/main-clean-baseline-${RID2}.txt"
# An unrelated per-issue checkpoint must never be touched by this helper.
: > "$BASELINE_DIR/issue-999.json"

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

# 7a. The dead peer's baseline is reaped with its registry entry (#4450).
if [[ -f "$BASELINE_DIR/main-clean-baseline-${RID2}.txt" ]]; then
    echo "FAIL: dead peer's baseline not pruned by peer scan" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: dead peer's baseline pruned by peer scan"
    PASS=$((PASS + 1))
fi

# 7b. The LIVE run's own baseline survives a peer scan.
if [[ -f "$BASELINE_DIR/main-clean-baseline-${RID1}.txt" ]]; then
    echo "PASS: live run's baseline untouched by peer scan"
    PASS=$((PASS + 1))
else
    echo "FAIL: live run's baseline was deleted by peer scan" >&2
    FAIL=$((FAIL + 1))
fi

# 8. list shows only the surviving run 1.
out=$("$REG" list)
if grep -q "^$RID1 " <<<"$out" && ! grep -q "$RID2" <<<"$out"; then
    echo "PASS: list shows surviving run only"
    PASS=$((PASS + 1))
else
    echo "FAIL: list unexpected after prune: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 8b. A third, still-live run registers a baseline that `cleanup RID1` must spare.
LIVE3=$(spawn_live); LIVE_PIDS+=("$LIVE3")
RID3=$("$REG" new --pid "$LIVE3")
: > "$BASELINE_DIR/main-clean-baseline-${RID3}.txt"

# 9. cleanup removes the run's own entry.
"$REG" cleanup "$RID1"
if [[ -f "$TMP_REPO/.loom/sweep-run/${RID1}.json" ]]; then
    echo "FAIL: cleanup did not remove own entry" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: cleanup removed own entry"
    PASS=$((PASS + 1))
fi

# 9a. cleanup also removes the run's own RUN_ID-keyed baseline (#4450).
if [[ -f "$BASELINE_DIR/main-clean-baseline-${RID1}.txt" ]]; then
    echo "FAIL: cleanup did not remove own main-clean baseline" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: cleanup removed own main-clean baseline"
    PASS=$((PASS + 1))
fi

# 9b. A live peer's baseline and registry entry survive another run's cleanup.
if [[ -f "$BASELINE_DIR/main-clean-baseline-${RID3}.txt" && -f "$TMP_REPO/.loom/sweep-run/${RID3}.json" ]]; then
    echo "PASS: live peer's baseline + entry untouched by another run's cleanup"
    PASS=$((PASS + 1))
else
    echo "FAIL: live peer's baseline or entry removed by another run's cleanup" >&2
    FAIL=$((FAIL + 1))
fi

# 9c. cleanup never touches per-issue checkpoints (that is the bulk path's job).
if [[ -f "$BASELINE_DIR/issue-999.json" ]]; then
    echo "PASS: per-issue checkpoint untouched by cleanup"
    PASS=$((PASS + 1))
else
    echo "FAIL: cleanup deleted an unrelated per-issue checkpoint" >&2
    FAIL=$((FAIL + 1))
fi

# Restore the empty-registry precondition the remaining cases assume.
"$REG" cleanup "$RID3"
kill "$LIVE3" 2>/dev/null
wait "$LIVE3" 2>/dev/null

# --------------------------------------------------------------------------
# #4691: the liveness PID must outlive the one-shot shell that ran `new`, and
# "not signallable" must never be mistaken for "dead".
# --------------------------------------------------------------------------

# 9d. `new` (no --pid) skips the ephemeral `<shell> -c …` wrapper it was invoked
#     from. An agent harness spawns one such shell PER TOOL CALL and reaps it
#     immediately, so recording its PID (the pre-fix bare `$PPID`) made every run
#     look dead to the very next peer scan. The trailing `:` keeps bash from
#     exec-optimizing the wrapper away, so the wrapper is a real intermediate.
EPH_FILE="$TMP_REPO/ephemeral.pid"
RID_FILE="$TMP_REPO/default.rid"
bash -c 'echo $$ > "$2"; "$1" new > "$3"; :' _ "$REG" "$EPH_FILE" "$RID_FILE"
EPH_PID=$(cat "$EPH_FILE")
RID_D=$(cat "$RID_FILE")
REG_PID=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' \
    "$TMP_REPO/.loom/sweep-run/${RID_D}.json")

if kill -0 "$EPH_PID" 2>/dev/null; then
    echo "SKIP: invoking one-shot shell $EPH_PID outlived the call; cannot assert" >&2
else
    echo "PASS: the one-shot invoking shell is already dead (the pre-fix handle)"
    PASS=$((PASS + 1))
    if [[ "$REG_PID" != "$EPH_PID" ]]; then
        echo "PASS: default liveness pid is not the ephemeral invoking shell"
        PASS=$((PASS + 1))
    else
        echo "FAIL: default liveness pid is the ephemeral invoking shell ($EPH_PID)" >&2
        FAIL=$((FAIL + 1))
    fi
    if kill -0 "$REG_PID" 2>/dev/null; then
        echo "PASS: default liveness pid ($REG_PID) is still alive after the call"
        PASS=$((PASS + 1))
    else
        echo "FAIL: default liveness pid $REG_PID is already dead after the call" >&2
        FAIL=$((FAIL + 1))
    fi
fi
"$REG" cleanup "$RID_D"

# 9e. A PID that EXISTS but cannot be signalled by this caller (POSIX EPERM) is
#     alive, not dead. PID 1 (launchd/init) is root-owned and always present, so
#     `kill -0 1` fails with EPERM for an unprivileged test run — and simply
#     succeeds when running as root, so the assertion holds either way.
LIVE4=$(spawn_live); LIVE_PIDS+=("$LIVE4")
RID_SELF=$("$REG" new --pid "$LIVE4")
RID_EPERM=$("$REG" new --pid 1)
: > "$BASELINE_DIR/main-clean-baseline-${RID_EPERM}.txt"
out=$("$REG" peers "$RID_SELF")
if grep -q "^$RID_EPERM 1 " <<<"$out"; then
    echo "PASS: unsignallable-but-existing pid reported as a live peer"
    PASS=$((PASS + 1))
else
    echo "FAIL: unsignallable-but-existing pid treated as dead, got: $out" >&2
    FAIL=$((FAIL + 1))
fi
if [[ -f "$TMP_REPO/.loom/sweep-run/${RID_EPERM}.json" \
    && -f "$BASELINE_DIR/main-clean-baseline-${RID_EPERM}.txt" ]]; then
    echo "PASS: unsignallable run's entry + baseline survive a peer scan"
    PASS=$((PASS + 1))
else
    echo "FAIL: unsignallable run's entry or baseline was pruned" >&2
    FAIL=$((FAIL + 1))
fi

# Restore the empty-registry precondition the remaining cases assume.
"$REG" cleanup "$RID_EPERM"
"$REG" cleanup "$RID_SELF"
kill "$LIVE4" 2>/dev/null
wait "$LIVE4" 2>/dev/null

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

# --------------------------------------------------------------------------
# #5896: same-PID stale-heartbeat classification. A `/clear` inside the
# long-lived `claude -p /loom:sweep …` orchestrator does not kill that OS
# process, so a pre-`/clear` registry entry and the post-`/clear` re-invoked
# run share the SAME liveness pid. `peers` must label the stale one distinctly
# from a genuine live peer (whether same-pid-fresh or a different pid).
# --------------------------------------------------------------------------

# 14. A same-PID entry with a STALE heartbeat is classified `stale-same-pid`,
#     not folded into an ordinary live-peer report — the exact `/clear` +
#     re-invoke zombie #5896 is about.
LIVE5=$(spawn_live); LIVE_PIDS+=("$LIVE5")
RID_NEW=$("$REG" new --pid "$LIVE5")  # the "new" post-/clear run
RID_OLD=$("$REG" new --pid "$LIVE5")  # the "old" pre-/clear zombie, same pid
# Force the OLD entry's heartbeat far into the past via a direct file edit
# (same technique already used above for baseline files) — this test must
# not depend on real time passing.
OLD_FILE="$TMP_REPO/.loom/sweep-run/${RID_OLD}.json"
cat > "$OLD_FILE" <<EOF
{
  "run_id": "$RID_OLD",
  "pid": $LIVE5,
  "timestamp": "2020-01-01T00:00:00Z",
  "heartbeat": "2020-01-01T00:00:00Z"
}
EOF
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_OLD $LIVE5 [^ ]+ [^ ]+ stale-same-pid:[0-9]+m$" <<<"$out"; then
    echo "PASS: same-PID stale-heartbeat entry classified stale-same-pid"
    PASS=$((PASS + 1))
else
    echo "FAIL: same-PID stale-heartbeat entry not classified as stale, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14a. The stale-same-pid entry is LABELED, not pruned — its pid is still
#      alive, only the heartbeat is stale, and #4691's keep-if-ambiguous bias
#      means this helper only ever deletes on confirmed PID death.
if [[ -f "$OLD_FILE" ]]; then
    echo "PASS: stale-same-pid entry is labeled, not deleted"
    PASS=$((PASS + 1))
else
    echo "FAIL: stale-same-pid entry was deleted (should only be labeled)" >&2
    FAIL=$((FAIL + 1))
fi

# 14b. A genuinely live same-process peer (FRESH heartbeat) must NOT be
#      misclassified as stale — the #4691 EPERM-fail-safe bias (never treat
#      an ambiguous/live case as dead) extended to the new same-PID path.
"$REG" heartbeat "$RID_OLD" >/dev/null
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_OLD $LIVE5 [^ ]+ [^ ]+ live-same-pid$" <<<"$out"; then
    echo "PASS: same-PID fresh-heartbeat entry classified live-same-pid, not stale"
    PASS=$((PASS + 1))
else
    echo "FAIL: same-PID fresh-heartbeat entry misclassified, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14c. A same-PID entry that is the caller's OWN run is still excluded via the
#      existing self-skip in cmd_peers (matched by RUN_ID) — not accidentally
#      caught (or exempted) by the new same-PID heartbeat logic.
out=$("$REG" peers "$RID_NEW")
if grep -q "$RID_NEW" <<<"$out"; then
    echo "FAIL: run listed itself as a peer under the new same-PID classification" >&2
    FAIL=$((FAIL + 1))
else
    echo "PASS: run still excludes itself under the new same-PID classification"
    PASS=$((PASS + 1))
fi

# 14d. A genuine live peer under a DIFFERENT pid with a FRESH heartbeat is
#      still classified plain `live` — neither the same-PID branch nor the
#      #6595 different-PID staleness check may swallow ordinary peer reports.
LIVE6=$(spawn_live); LIVE_PIDS+=("$LIVE6")
RID_DIFF=$("$REG" new --pid "$LIVE6")
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ live$" <<<"$out"; then
    echo "PASS: different-pid fresh-heartbeat peer still classified plain 'live'"
    PASS=$((PASS + 1))
else
    echo "FAIL: different-pid fresh-heartbeat peer misclassified, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# --------------------------------------------------------------------------
# #6595: DIFFERENT-pid stale-heartbeat classification. A sweep interrupted
# inside a long-lived interactive session leaves an entry whose pid (that
# session) stays alive for hours — under a pid unrelated to the next sweep's,
# so the #5896 same-PID branch never sees it and it warned as a plain `live`
# peer for the session's entire lifetime.
# --------------------------------------------------------------------------

# 14f. The reported false positive: entry pid alive, DIFFERENT from the
#      caller's, heartbeat far in the past → `stale-heartbeat:Nm`, not `live`.
DIFF_FILE="$TMP_REPO/.loom/sweep-run/${RID_DIFF}.json"
cat > "$DIFF_FILE" <<EOF
{
  "run_id": "$RID_DIFF",
  "pid": $LIVE6,
  "timestamp": "2020-01-01T00:00:00Z",
  "heartbeat": "2020-01-01T00:00:00Z"
}
EOF
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ stale-heartbeat:[0-9]+m$" <<<"$out"; then
    echo "PASS: different-pid stale-heartbeat entry classified stale-heartbeat"
    PASS=$((PASS + 1))
else
    echo "FAIL: different-pid stale-heartbeat entry not flagged stale, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14g. That entry is LABELED, not pruned — its pid is alive, and #4691's
#      keep-when-ambiguous bias means only confirmed PID death may delete.
if [[ -f "$DIFF_FILE" ]]; then
    echo "PASS: stale-heartbeat entry is labeled, not deleted"
    PASS=$((PASS + 1))
else
    echo "FAIL: stale-heartbeat entry was deleted (should only be labeled)" >&2
    FAIL=$((FAIL + 1))
fi

# 14h. A heartbeat refresh clears the stale label — the entry goes straight
#      back to plain `live` once its sweep proves it is still driving work.
"$REG" heartbeat "$RID_DIFF" >/dev/null
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ live$" <<<"$out"; then
    echo "PASS: heartbeat refresh returns a stale-heartbeat entry to 'live'"
    PASS=$((PASS + 1))
else
    echo "FAIL: refreshed different-pid entry not back to 'live', got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14i. Backward compat / fail-safe: a pre-#5896 entry has NO "heartbeat" field
#      and its writer never refreshes one, so its (old) registration time says
#      nothing about whether it is still sweeping. It must stay plain `live`,
#      never be manufactured into a stale label from the timestamp fallback.
cat > "$DIFF_FILE" <<EOF
{
  "run_id": "$RID_DIFF",
  "pid": $LIVE6,
  "timestamp": "2020-01-01T00:00:00Z"
}
EOF
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ live$" <<<"$out"; then
    echo "PASS: pre-#5896 entry with no heartbeat field stays plain 'live'"
    PASS=$((PASS + 1))
else
    echo "FAIL: heartbeat-less entry misclassified, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14j. An UNPARSEABLE heartbeat is "unknown age", never "stale" (same fail-safe
#      the same-PID path already applies).
cat > "$DIFF_FILE" <<EOF
{
  "run_id": "$RID_DIFF",
  "pid": $LIVE6,
  "timestamp": "2020-01-01T00:00:00Z",
  "heartbeat": "not-a-timestamp"
}
EOF
out=$("$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ live$" <<<"$out"; then
    echo "PASS: unparseable heartbeat on a different-pid entry stays 'live'"
    PASS=$((PASS + 1))
else
    echo "FAIL: unparseable heartbeat manufactured a stale label, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14k. SWEEP_RUN_HEARTBEAT_STALE_SECS governs the different-PID threshold too:
#      a very large threshold keeps an old-heartbeat peer `live`.
cat > "$DIFF_FILE" <<EOF
{
  "run_id": "$RID_DIFF",
  "pid": $LIVE6,
  "timestamp": "2020-01-01T00:00:00Z",
  "heartbeat": "2020-01-01T00:00:00Z"
}
EOF
out=$(SWEEP_RUN_HEARTBEAT_STALE_SECS=999999999999 "$REG" peers "$RID_NEW")
if grep -qE "^$RID_DIFF $LIVE6 [^ ]+ [^ ]+ live$" <<<"$out"; then
    echo "PASS: SWEEP_RUN_HEARTBEAT_STALE_SECS raises the different-pid threshold"
    PASS=$((PASS + 1))
else
    echo "FAIL: stale threshold override ignored for different-pid entry, got: $out" >&2
    FAIL=$((FAIL + 1))
fi

# 14e. `heartbeat` on an unregistered RUN_ID is a usage error (exit 1), and on
#      a known RUN_ID leaves `pid`/`timestamp` unchanged (only `heartbeat`
#      advances).
assert_exit "heartbeat on unknown RUN_ID exits 1" 1 "$REG" heartbeat "sweep-nonexistent"
assert_exit "heartbeat requires a RUN_ID arg" 1 "$REG" heartbeat
ORIG_TS=$(sed -n 's/.*"timestamp"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TMP_REPO/.loom/sweep-run/${RID_NEW}.json")
"$REG" heartbeat "$RID_NEW" >/dev/null
NEW_TS=$(sed -n 's/.*"timestamp"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$TMP_REPO/.loom/sweep-run/${RID_NEW}.json")
NEW_PID=$(sed -n 's/.*"pid"[[:space:]]*:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$TMP_REPO/.loom/sweep-run/${RID_NEW}.json")
assert_eq "heartbeat leaves timestamp unchanged" "$ORIG_TS" "$NEW_TS"
assert_eq "heartbeat leaves pid unchanged" "$LIVE5" "$NEW_PID"

# Restore the empty-registry precondition the remaining cases assume.
"$REG" cleanup "$RID_NEW"
"$REG" cleanup "$RID_OLD"
"$REG" cleanup "$RID_DIFF"
kill "$LIVE5" "$LIVE6" 2>/dev/null
wait "$LIVE5" "$LIVE6" 2>/dev/null

echo
echo "Results: $PASS passed, $FAIL failed"
[[ $FAIL -eq 0 ]] || exit 1
