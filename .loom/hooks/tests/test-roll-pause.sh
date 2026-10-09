#!/usr/bin/env bash
# test-roll-pause.sh — hermetic tests of the daemon-roll pause hook's shell glue
# (issue #10830): defaults/hooks/roll-pause.sh, its call from
# guard-codex-bridge.sh, and the Stop guard's pause exemption in
# guard-background-subagents.sh.
#
# The park / ledger / safe-point logic is `loom-daemon roll-pause hook`, tested
# in Rust (loom-daemon/src/roll_pause/tests.rs). This suite drives the shell
# entry points against a FAKE binary (LOOM_ROLL_PAUSE_BIN) so it needs no
# build, and checks what only the shell can get wrong:
#   - an in-session agent (no LOOM_DAEMON_ITEM_ID) never starts the binary;
#   - a missing or too-old binary fails open (no output, exit 0);
#   - the hook passes stdin and the harness pid through, and its deny reaches
#     stdout; `active` passes the binary's exit status through;
#   - the Codex bridge denies a parked call before any other guard runs, runs
#     the hook without a ledger and with a park window under its 30 s timeout,
#     and ignores the hook entirely without an item id;
#   - the Stop guard lets a paused session stop even with an outstanding
#     background subagent in its transcript.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HOOKS="$(cd "$HERE/.." && pwd)"
PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); printf '  PASS: %s\n' "$1"; }
fail() { FAIL=$((FAIL + 1)); printf '  FAIL: %s\n' "$1"; }
check() { if [[ "$2" == "$3" ]]; then pass "$1"; else fail "$1 (expected [$3], got [$2])"; fi; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
CALLS="$TMP/calls.log"

# A fake `loom-daemon`: records its argv, env and stdin; denies a PreToolUse
# when $TMP/requested exists; `roll-pause active` exits 0 when it exists.
FAKE="$TMP/fake-daemon"
cat >"$FAKE" <<EOF
#!/usr/bin/env bash
payload="\$(cat)"
printf 'argv=%s ledger=%s park=%s runtime=%s payload=%s\n' "\$*" "\${LOOM_ROLL_PAUSE_LEDGER:-}" "\${LOOM_ROLL_PAUSE_PARK_SECS:-}" "\${LOOM_ROLL_PAUSE_RUNTIME:-}" "\$payload" >>"$CALLS"
case "\$2" in
  active) [[ -e "$TMP/requested" ]]; exit \$? ;;
  hook) [[ -e "$TMP/requested" ]] && printf '%s\n' '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"Loom: paused for a daemon roll. This tool call did not run."}}'; exit 0 ;;
esac
exit 0
EOF
OLD="$TMP/old-daemon"
printf '#!/usr/bin/env bash\necho "error: unrecognized subcommand roll-pause" >&2\nexit 2\n' >"$OLD"
chmod +x "$FAKE" "$OLD"

PAYLOAD='{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_use_id":"t1","tool_input":{"command":"ls"},"cwd":"/tmp"}'

echo "roll-pause.sh"
out="$(printf '%s' "$PAYLOAD" | env -u LOOM_DAEMON_ITEM_ID LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/roll-pause.sh")"; rc=$?
check "in-session: hook exits 0" "$rc" "0"
check "in-session: hook prints nothing" "$out" ""
check "in-session: the binary is never started" "$(cat "$CALLS" 2>/dev/null)" ""
env -u LOOM_DAEMON_ITEM_ID LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/roll-pause.sh" active </dev/null; rc=$?
check "in-session: active is false (1)" "$rc" "1"

out="$(printf '%s' "$PAYLOAD" | LOOM_DAEMON_ITEM_ID=item-1 LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/roll-pause.sh")"; rc=$?
check "daemon agent, no request: allow (exit 0, no output)" "$rc:$out" "0:"
grep -q "argv=roll-pause hook --harness-pid [0-9]" "$CALLS" && pass "hook passes the harness pid" || fail "hook passes the harness pid"
grep -q '"tool_use_id":"t1"' "$CALLS" && pass "hook passes the payload on stdin" || fail "hook passes the payload on stdin"

touch "$TMP/requested"
out="$(printf '%s' "$PAYLOAD" | LOOM_DAEMON_ITEM_ID=item-1 LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/roll-pause.sh")"; rc=$?
check "requested: hook exits 0" "$rc" "0"
[[ "$out" == *'"permissionDecision":"deny"'* ]] && pass "requested: the deny reaches stdout" || fail "requested: the deny reaches stdout ($out)"
LOOM_DAEMON_ITEM_ID=item-1 LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/roll-pause.sh" active </dev/null; rc=$?
check "requested: active is true (0)" "$rc" "0"

out="$(printf '%s' "$PAYLOAD" | LOOM_DAEMON_ITEM_ID=item-1 LOOM_ROLL_PAUSE_BIN="$OLD" bash "$HOOKS/roll-pause.sh" 2>&1)"; rc=$?
check "an older binary fails open: exit 0, nothing printed" "$rc:$out" "0:"
out="$(printf '%s' "$PAYLOAD" | LOOM_DAEMON_ITEM_ID=item-1 LOOM_ROLL_PAUSE_BIN="$TMP/missing" bash "$HOOKS/roll-pause.sh" 2>&1)"; rc=$?
check "a missing binary fails open: exit 0, nothing printed" "$rc:$out" "0:"
rm -f "$TMP/requested"

echo "guard-codex-bridge.sh"
if command -v jq >/dev/null 2>&1; then
    REPO="$TMP/repo"; mkdir -p "$REPO" && git -C "$REPO" init -q
    CODEX='{"hook_event_name":"PreToolUse","tool_name":"exec_command","tool_use_id":"c1","tool_input":{"cmd":"ls"},"cwd":"'"$REPO"'","session_id":"s","model":"m","permission_mode":"default","transcript_path":"","turn_id":"1"}'
    : >"$CALLS"
    touch "$TMP/requested"
    out="$(cd "$REPO" && printf '%s' "$CODEX" | LOOM_DAEMON_ITEM_ID=cx-1 LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/guard-codex-bridge.sh" --project-root "$REPO")"
    [[ "$out" == *'paused for a daemon roll'* ]] && pass "a parked Codex call is denied with the pause reason" || fail "parked Codex call denied ($out)"
    grep -q 'ledger=0 park=20 runtime=codex' "$CALLS" && pass "Codex runs the hook ledger-free with a 20 s park" || fail "Codex hook env ($(cat "$CALLS"))"
    : >"$CALLS"
    out="$(cd "$REPO" && printf '%s' "$CODEX" | env -u LOOM_DAEMON_ITEM_ID LOOM_ROLL_PAUSE_BIN="$FAKE" bash "$HOOKS/guard-codex-bridge.sh" --project-root "$REPO")"
    [[ "$out" != *'paused for a daemon roll'* ]] && pass "an in-session Codex call is never parked" || fail "in-session Codex call parked"
    check "an in-session Codex call never starts the binary" "$(cat "$CALLS")" ""
    rm -f "$TMP/requested"
else
    echo "  SKIP: jq not available"
fi

echo "guard-background-subagents.sh"
TRANSCRIPT="$TMP/transcript.jsonl"
printf '%s\n' '{"type":"assistant","message":{"content":[{"type":"tool_use","id":"toolu_task","name":"Task","input":{"description":"x","prompt":"y"}}]}}' >"$TRANSCRIPT"
STOP='{"session_id":"s","transcript_path":"'"$TRANSCRIPT"'","stop_hook_active":false,"hook_event_name":"Stop"}'
out="$(printf '%s' "$STOP" | LOOM_HEADLESS_SESSION=1 LOOM_DAEMON_ITEM_ID=item-2 LOOM_ROLL_PAUSE_BIN="$FAKE" LOOM_GUARD_BACKGROUND_SUBAGENTS=1 bash "$HOOKS/guard-background-subagents.sh")"
[[ "$(printf '%s' "$out" | tr -d ' \n')" == *'"decision":"block"'* ]] && pass "without a pause, an outstanding subagent still blocks the stop" || fail "baseline block ($out)"
touch "$TMP/requested"
out="$(printf '%s' "$STOP" | LOOM_HEADLESS_SESSION=1 LOOM_DAEMON_ITEM_ID=item-2 LOOM_ROLL_PAUSE_BIN="$FAKE" LOOM_GUARD_BACKGROUND_SUBAGENTS=1 bash "$HOOKS/guard-background-subagents.sh")"; rc=$?
check "a paused session may stop (exit 0, no block)" "$rc:$out" "0:"

echo
echo "test-roll-pause.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
