#!/usr/bin/env bash
# Test suite for defaults/hooks/guard-background-subagents.sh (issues #4257, #4389)
#
# Usage: ./defaults/hooks/tests/test-guard-background-subagents.sh
#
# Covers the Stop-hook mechanical backstop for the #3822/#4257/#4389 hazard: an
# orchestrator ending its turn in headless `claude -p` mode kills every
# still-running background Task subagent AND every outstanding background
# Bash task (`run_in_background: true`). This hook scans the transcript
# JSONL for (1) `Task` tool_use entries with no matching tool_result, and
# (2) background-Bash tool_use entries with no matching `<task-notification>`
# completion event, and blocks the stop (once) when it finds either.
#
#   - unresolved Task tool_use (no matching tool_result) -> block
#   - all Task tool_use entries resolved -> allow (silent)
#   - no Task tool_use at all -> allow (silent)
#   - stop_hook_active=true -> allow unconditionally (loop guard), even with
#     an unresolved Task still in the transcript
#   - missing / unreadable transcript_path -> allow (fail-open)
#   - unparseable transcript content -> allow (fail-open)
#   - outstanding background Bash task (dispatch + immediate ack only, no
#     later task-notification echoing the tool-use-id) -> block (#4389)
#   - completed background Bash task (task-notification observed in the REAL
#     harness shapes — `queue-operation` top-level .content and `attachment`
#     .attachment.prompt, per #4482) -> allow, and re-allows on a second stop
#     sequence over the same transcript (no one-shot/stateful matching)
#   - outstanding background Bash task + stop_hook_active=true -> allow
#     (loop guard applies identically)
#   - Monitor resolution shapes (#4696): a fired <task-notification> keyed on
#     <task-id> (Monitor notifications never carry <tool-use-id>), an explicit
#     TaskStop naming the task id, the Monitor's own configured timeout
#     elapsing, and a failed arming call all retire a timer -- while a
#     persistent timer with no resolution signal, and a timeout that has not
#     yet elapsed, still block. Retired timers are never re-flagged on a later
#     stop sequence over the same transcript.
#   - guards.backgroundSubagents / LOOM_GUARD_BACKGROUND_SUBAGENTS toggle
#     (env beats config; config beats default-on)
#   - real "Agent" dispatch tool name detection (#5086): the harness names the
#     async subagent-dispatch tool "Agent", not "Task" -- a dispatched Agent
#     with only its immediate launch ack ("Async agent launched successfully
#     ... agentId: <ID> ...") still blocks (the ack must NOT itself count as
#     resolution, else the #4389 hazard recurs on this tool); a LATER, distinct
#     tool_result on the same id, or a non-error TERMINAL TaskOutput poll of
#     the recovered agentId, resolves it; a <status>running</status> poll does
#     NOT; existing Task-named fixtures continue to pass unmodified
#   - jq absent -> allow (fail-open)
#   - contract: block output is valid JSON with decision=="block" and a
#     non-empty reason; exit code is always 0
#
# The hook under test is the canonical source at defaults/ (the version-
# controlled source of truth), copied into an isolated temp git tree so the
# hook's MAIN_ROOT (used only for the config-toggle lookup) resolves there.
# Exit 0 = all pass, 1 = fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
SRC_HOOK="$REPO_ROOT/defaults/hooks/guard-background-subagents.sh"

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TMPROOT="$(mktemp -d)"
trap 'rm -rf "$TMPROOT"' EXIT
git init -q "$TMPROOT"
mkdir -p "$TMPROOT/.loom/hooks"
cp "$SRC_HOOK" "$TMPROOT/.loom/hooks/guard-background-subagents.sh"
chmod +x "$TMPROOT/.loom/hooks/guard-background-subagents.sh"
HOOK="$TMPROOT/.loom/hooks/guard-background-subagents.sh"

pass() { PASS=$((PASS + 1)); TOTAL=$((TOTAL + 1)); printf "${GREEN}PASS${NC} %s\n" "$1"; }
fail() { FAIL=$((FAIL + 1)); TOTAL=$((TOTAL + 1)); printf "${RED}FAIL${NC} %s\n" "$1"; }

# Write a transcript JSONL fixture. Args: <path> <line>...
write_transcript() {
    local path="$1"; shift
    : > "$path"
    for line in "$@"; do
        printf '%s\n' "$line" >> "$path"
    done
}

TASK_USE_UNRESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_01","name":"Task","input":{}}]}}'
TASK_USE_RESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_02","name":"Task","input":{}}]}}'
TASK_RESULT_02='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_02","content":"done"}]}}'
NON_TASK_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_03","name":"Bash","input":{}}]}}'

# Real async-subagent-dispatch tool name (issue #5086): the harness names this
# tool "Agent", not "Task". Dispatching it returns an IMMEDIATE launch-ack
# tool_result on the SAME tool_use id the real completion later arrives on —
# that ack text must NEVER itself count as resolution (else the #4389
# false-negative hazard recurs on this tool).
AGENT5086_USE_UNRESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent01","name":"Agent","input":{"prompt":"implement issue #1"}}]}}'
AGENT5086_ACK_UNRESOLVED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent01","content":"Async agent launched successfully. agentId: aagent01. Use SendMessage with to: aagent01 to communicate. You will be notified automatically when it completes."}]}}'

# (b) a LATER, distinct tool_result on the SAME id (the real completion) —
# the harness may emit a second tool_result block for the same tool_use id
# once the dispatched agent actually finishes.
AGENT5086_USE_COMPLETED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent02","name":"Agent","input":{"prompt":"implement issue #2"}}]}}'
AGENT5086_ACK_COMPLETED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent02","content":"Async agent launched successfully. agentId: aagent02. Use SendMessage with to: aagent02 to communicate. You will be notified automatically when it completes."}]}}'
AGENT5086_RESULT_COMPLETED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent02","content":"Work complete: opened PR #5555 for issue #2."}]}}'

# (c) resolved ONLY via a blocking TaskOutput poll of the agentId recovered
# from the launch ack — no second tool_result ever lands on the dispatch id
# itself. A non-error, TERMINAL (<status>completed</status>) poll result
# resolves it; a <status>running</status> poll result must NOT.
AGENT5086_USE_POLLED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent03","name":"Agent","input":{"prompt":"implement issue #3"}}]}}'
AGENT5086_ACK_POLLED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent03","content":"Async agent launched successfully. agentId: aagent03. Use SendMessage with to: aagent03 to communicate. You will be notified automatically when it completes."}]}}'
AGENT5086_POLL_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_poll03","name":"TaskOutput","input":{"agentId":"aagent03","block":true,"timeout":600}}]}}'
AGENT5086_POLL_RESULT_TERMINAL='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_poll03","content":"<retrieval_status>success</retrieval_status>\n<status>completed</status>\nWork complete: opened PR #5556 for issue #3."}]}}'
AGENT5086_POLL_RESULT_RUNNING='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_poll03","content":"<retrieval_status>timeout</retrieval_status>\n<status>running</status>\nStill working..."}]}}'

# Background Bash (issue #4389 — the #4257 recurrence via run_in_background)
# fixtures. A background dispatch gets an IMMEDIATE tool_result ack (NOT
# completion) at dispatch time; the real completion arrives later as a
# <task-notification> whose <tool-use-id> echoes the dispatch id.
BG_USE_UNRESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg01","name":"Bash","input":{"command":"sleep 100","run_in_background":true}}]}}'
BG_ACK_UNRESOLVED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg01","content":"Command running in background with ID: bgtest01. Output is being written to: /tmp/bgtest01.output. You will be notified when it completes."}]}}'
BG_USE_RESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg02","name":"Bash","input":{"command":"sleep 1","run_in_background":true}}]}}'
BG_ACK_RESOLVED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg02","content":"Command running in background with ID: bgtest02. Output is being written to: /tmp/bgtest02.output. You will be notified when it completes."}]}}'
# Completion notification in the REAL harness shapes (issue #4482). The prior
# fixture used a `type==\"user\"` string-content shape that does NOT occur in a
# real transcript, so it re-confirmed the (broken) code instead of testing it.
# Verified against live transcripts, a background-Bash `<task-notification>`
# lands as one (or both) of these top-level entry shapes:
#   shape 1: {"type":"queue-operation", "content":"<task-notification>...</task-notification>"}
#   shape 2: {"type":"attachment","attachment":{"commandMode":"task-notification","prompt":"<task-notification>...</task-notification>"}}
BG_NOTIFICATION_QUEUEOP='{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>bgtest02</task-id>\n<tool-use-id>toolu_bg02</tool-use-id>\n<output-file>/tmp/bgtest02.output</output-file>\n<status>completed</status>\n<summary>Background command completed (exit code 0)</summary>\n</task-notification>"}'
BG_NOTIFICATION_ATTACHMENT='{"type":"attachment","attachment":{"type":"queued_command","commandMode":"task-notification","prompt":"<task-notification>\n<task-id>bgtest02</task-id>\n<tool-use-id>toolu_bg02</tool-use-id>\n<output-file>/tmp/bgtest02.output</output-file>\n<status>completed</status>\n<summary>Background command completed (exit code 0)</summary>\n</task-notification>"}}'

# Armed Monitor / ScheduleWakeup (issue #4462 — the transport-failure backoff
# strand) fixtures. Arming a Monitor returns an IMMEDIATE tool_result ack that
# is NOT the fire event.
#
# CRITICAL (issue #4696): a Monitor's fired-event <task-notification> carries
# ONLY <task-id> — verified against every live Monitor notification on a real
# host, it NEVER emits the <tool-use-id> tag a background-Bash completion does.
# The fixtures below therefore use the REAL harness shapes:
#   - ack:          "Monitor started (task <ID>, timeout <N>ms). …"
#                   "Monitor started (task <ID>, persistent — runs until TaskStop or session end). …"
#   - fired event:  <task-notification><task-id><ID></task-id><summary>Monitor event: …</summary><event>…</event></task-notification>
#   - TaskStop:     tool_use {name:"TaskStop", input:{task_id:"<ID>"}} +
#                   tool_result '{"message":"Successfully stopped task: <ID> (…)","task_id":"<ID>",…}'
# The pre-#4696 fixture synthesised a <tool-use-id> tag into the Monitor
# notification, which re-confirmed the broken matcher exactly the way the
# pre-#4482 background-Bash fixture did.

# Persistent Monitor, armed and never retired -> genuinely still armed.
MON_USE_UNRESOLVED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_mon01","name":"Monitor","input":{"command":"sleep 90 && echo backoff-complete","persistent":true}}]}}'
MON_ACK_UNRESOLVED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_mon01","content":"Monitor started (task bmon0001, persistent — runs until TaskStop or session end). You will be notified on each event. Keep working — do not poll or sleep."}]}}'

# Monitor that fires (real notification shape: <task-id> only, no <tool-use-id>).
MON_USE_RESOLVED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_mon02","name":"Monitor","input":{"command":"sleep 1 && echo done","persistent":true}}]}}'
MON_ACK_RESOLVED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_mon02","content":"Monitor started (task bmon0002, persistent — runs until TaskStop or session end). You will be notified on each event."}]}}'
MON_NOTIFICATION_QUEUEOP='{"type":"queue-operation","operation":"enqueue","timestamp":"2026-07-30T12:05:00.000Z","content":"<task-notification>\n<task-id>bmon0002</task-id>\n<summary>Monitor event: \"backoff window elapsed\"</summary>\n<event>backoff-complete</event>\n</task-notification>"}'
MON_NOTIFICATION_ATTACHMENT='{"type":"attachment","timestamp":"2026-07-30T12:05:00.000Z","attachment":{"type":"queued_command","commandMode":"task-notification","prompt":"<task-notification>\n<task-id>bmon0002</task-id>\n<summary>Monitor event: \"backoff window elapsed\"</summary>\n<event>backoff-complete</event>\n</task-notification>"}}'

# TaskStop of the fired monitor (real tool_use + tool_result shapes).
MON_TASKSTOP_USE='{"type":"assistant","timestamp":"2026-07-30T12:06:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_stop02","name":"TaskStop","input":{"task_id":"bmon0002"}}]}}'
MON_TASKSTOP_RESULT='{"type":"user","timestamp":"2026-07-30T12:06:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_stop02","content":"{\"message\":\"Successfully stopped task: bmon0002 (sleep 1 && echo done)\",\"task_id\":\"bmon0002\",\"task_type\":\"local_bash\",\"command\":\"sleep 1 && echo done\"}"}]}}'

# TaskStop of the OTHER (still-armed) monitor — used to prove TaskStop alone,
# with no fired event and no timeout, retires a timer.
MON_TASKSTOP_USE_01='{"type":"assistant","timestamp":"2026-07-30T12:06:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_stop01","name":"TaskStop","input":{"task_id":"bmon0001"}}]}}'
MON_TASKSTOP_RESULT_01='{"type":"user","timestamp":"2026-07-30T12:06:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_stop01","content":"{\"message\":\"Successfully stopped task: bmon0001 (sleep 90 && echo backoff-complete)\",\"task_id\":\"bmon0001\",\"task_type\":\"local_bash\",\"command\":\"sleep 90 && echo backoff-complete\"}"}]}}'

# Non-persistent Monitor with its own timeout, armed long ago -> self-expired.
MON_USE_TIMEOUT='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_mon03","name":"Monitor","input":{"command":"sleep 90 && echo backoff-complete","timeout_ms":180000}}]}}'
MON_ACK_TIMEOUT='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_mon03","content":"Monitor started (task bmon0003, timeout 180000ms). You will be notified on each event."}]}}'
MON_NOTIFICATION_TIMEOUT='{"type":"queue-operation","operation":"enqueue","timestamp":"2026-07-30T12:03:01.000Z","content":"<task-notification>\n<task-id>bmon0003</task-id>\n<summary>Monitor event: \"backoff window elapsed\"</summary>\n<event>backoff-complete</event>\n</task-notification>"}'

# Failed arm: the Monitor call itself errored, so no timer exists at all.
MON_USE_ERRORED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_mon04","name":"Monitor","input":{"condition":"nope"}}]}}'
MON_ACK_ERRORED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_mon04","is_error":true,"content":"<tool_use_error>InputValidationError: Monitor failed due to the following issues:\nAn unexpected parameter `condition` was provided</tool_use_error>"}]}}'

# ScheduleWakeup is the sibling tool name the same detector must also catch.
# Its arming ack is a DIFFERENT shape from Monitor's (#4696): a fired wakeup
# re-invokes the session and leaves no task id and no task-notification, so a
# wakeup is retired only by its own delay elapsing, by a later `{stop: true}`
# cancel, or by the arming call erroring.
WAKE_USE_UNRESOLVED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_wake01","name":"ScheduleWakeup","input":{"seconds":90}}]}}'
WAKE_USE_ELAPSED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_wake02","name":"ScheduleWakeup","input":{"delaySeconds":1500,"prompt":"night-shift tick","reason":"periodic health tick"}}]}}'
WAKE_ACK_ELAPSED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_wake02","content":"Next wakeup scheduled for 12:25:00 (in 1500s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives."}]}}'
WAKE_CANCEL_USE='{"type":"assistant","timestamp":"2026-07-30T12:10:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_wake03","name":"ScheduleWakeup","input":{"stop":true}}]}}'
WAKE_CANCEL_RESULT='{"type":"user","timestamp":"2026-07-30T12:10:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_wake03","content":"Loop stopped — cancelled 1 pending wakeup(s); no further dynamic-loop wakeups scheduled. If you armed a Monitor for this loop, TaskStop it now; otherwise nothing more to do this turn."}]}}'
WAKE_USE_ERRORED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_wake04","name":"ScheduleWakeup","input":{"delaySeconds":1500}}]}}'
WAKE_ACK_ERRORED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_wake04","is_error":true,"content":"`prompt` is required when `stop` is not true."}]}}'

# Background Bash task that was explicitly TaskStop'd instead of completing
# (issue #4696 — the same false-positive class as the Monitor gap).
BG_USE_STOPPED='{"type":"assistant","timestamp":"2026-07-30T12:00:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg03","name":"Bash","input":{"command":"tail -f /tmp/x","run_in_background":true}}]}}'
BG_ACK_STOPPED='{"type":"user","timestamp":"2026-07-30T12:00:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg03","content":"Command running in background with ID: bgtest03. Output is being written to: /tmp/bgtest03.output. You will be notified when it completes."}]}}'
BG_TASKSTOP_USE='{"type":"assistant","timestamp":"2026-07-30T12:01:00.000Z","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_stop03","name":"TaskStop","input":{"task_id":"bgtest03"}}]}}'
BG_TASKSTOP_RESULT='{"type":"user","timestamp":"2026-07-30T12:01:01.000Z","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_stop03","content":"{\"message\":\"Successfully stopped task: bgtest03 (tail -f /tmp/x)\",\"task_id\":\"bgtest03\",\"task_type\":\"local_bash\",\"command\":\"tail -f /tmp/x\"}"}]}}'

# --- issue #5013 fixtures: background Bash completions the pre-#5013 matcher
# missed (the constant "1 outstanding" false positive) --------------------------
#
# (u1) A background Bash task whose completion `<task-notification>` carries ONLY
# `<task-id>` (no `<tool-use-id>`) — the Monitor-shaped notification that
# `<tool-use-id>`-only matching never observed. Uses a DISTINCT dispatch id.
BG_USE_TASKID_ONLY='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg10","name":"Bash","input":{"command":"sleep 1","run_in_background":true}}]}}'
BG_ACK_TASKID_ONLY='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg10","content":"Command running in background with ID: bgtest10. Output is being written to: /tmp/bgtest10.output. You will be notified when it completes."}]}}'
BG_NOTIFICATION_TASKID_ONLY='{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>bgtest10</task-id>\n<output-file>/tmp/bgtest10.output</output-file>\n<status>completed</status>\n<summary>Background command completed (exit code 0)</summary>\n</task-notification>"}'

# (u2) A background Bash task awaited via a blocking BashOutput read (keyed on
# bash_id) whose result is a non-error output — and NO task-notification at all.
BG_USE_READ='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg11","name":"Bash","input":{"command":"make build","run_in_background":true}}]}}'
BG_ACK_READ='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg11","content":"Command running in background with ID: bgtest11. Output is being written to: /tmp/bgtest11.output. You will be notified when it completes."}]}}'
BG_READ_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_read11","name":"BashOutput","input":{"bash_id":"bgtest11"}}]}}'
BG_READ_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_read11","content":"<status>completed</status>\nbuild finished successfully\n"}]}}'

# (u3) Async agent (Task subagent) dispatched and awaited via a blocking
# TaskOutput. The Task dispatch gets an immediate ack keyed to its own id (so
# pattern 1 does not flag it), then a TaskOutput read of the same task id
# returns the completion. This is the "M async agents awaited via TaskOutput"
# arm of the acceptance-criteria regression scenario. It must NEVER be counted
# as a background Bash task.
AGENT_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_task20","name":"Task","input":{"subagent_type":"loom-builder","run_in_background":true}}]}}'
AGENT_ACK='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_task20","content":"Task started in background with ID: agent20. You will be notified when it completes."}]}}'
AGENT_READ_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_read20","name":"TaskOutput","input":{"task_id":"agent20"}}]}}'
AGENT_READ_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_read20","content":"Subagent completed: opened PR #1234."}]}}'

# A foreground-only final turn (a plain Bash tool_use with no run_in_background).
FG_FINAL_TURN='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_fg30","name":"Bash","input":{"command":"gh issue create"}}]}}'
FG_FINAL_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_fg30","content":"https://github.com/x/y/issues/1"}]}}'

# Build stdin JSON. Args: <transcript_path> <stop_hook_active>
make_input() {
    local transcript="$1" active="${2:-false}"
    jq -n --arg tp "$transcript" --argjson active "$active" \
        '{session_id: "test", transcript_path: $tp, stop_hook_active: $active, hook_event_name: "Stop"}'
}

run_hook() {
    local transcript="$1" active="${2:-false}"
    shift; [[ $# -gt 0 ]] && shift
    local exit_code=0 output
    output=$(cd "$TMPROOT" && env "$@" bash "$HOOK" < <(make_input "$transcript" "$active") 2>/dev/null) || exit_code=$?
    printf '%s|%s' "$exit_code" "$output"
}

assert_allow() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" == "0" && -z "$out" ]]; then
        pass "$desc"
    else
        fail "$desc (expected exit 0 + empty output, got exit=$code output=$out)"
    fi
}

assert_block() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (expected exit 0 with block JSON, got NONZERO exit=$code)"
        return
    fi
    local decision reason
    decision=$(echo "$out" | jq -r '.decision // empty' 2>/dev/null || true)
    reason=$(echo "$out" | jq -r '.reason // empty' 2>/dev/null || true)
    if [[ "$decision" == "block" && -n "$reason" ]]; then
        pass "$desc"
    else
        fail "$desc (expected decision=block + non-empty reason, got: $out)"
    fi
}

echo "=== guard-background-subagents.sh tests (#4257) ==="

# (a) unresolved Task tool_use -> block
T1="$TMPROOT/transcript-unresolved.jsonl"
write_transcript "$T1" "$TASK_USE_UNRESOLVED"
result=$(run_hook "$T1" false)
assert_block "(a) unresolved Task tool_use -> block" "$result"

# (b) all Task tool_use resolved -> allow
T2="$TMPROOT/transcript-resolved.jsonl"
write_transcript "$T2" "$TASK_USE_RESOLVED" "$TASK_RESULT_02"
result=$(run_hook "$T2" false)
assert_allow "(b) resolved Task tool_use -> allow" "$result"

# (c) no Task tool_use at all -> allow
T3="$TMPROOT/transcript-no-task.jsonl"
write_transcript "$T3" "$NON_TASK_USE"
result=$(run_hook "$T3" false)
assert_allow "(c) no Task tool_use -> allow" "$result"

# (d) stop_hook_active=true -> allow unconditionally (loop guard), even with
# an unresolved Task still present
result=$(run_hook "$T1" true)
assert_allow "(d) stop_hook_active=true -> allow (loop guard)" "$result"

# (e) missing transcript_path file -> allow (fail-open)
result=$(run_hook "$TMPROOT/does-not-exist.jsonl" false)
assert_allow "(e) missing transcript file -> allow (fail-open)" "$result"

# (f) unparseable transcript content -> allow (fail-open)
T4="$TMPROOT/transcript-garbage.jsonl"
printf 'not json at all\n{"also": "not", unterminated\n' > "$T4"
result=$(run_hook "$T4" false)
assert_allow "(f) unparseable transcript -> allow (fail-open)" "$result"

# (g) empty transcript_path in input -> allow
output=$(cd "$TMPROOT" && jq -n '{session_id:"t", transcript_path:"", stop_hook_active:false}' | bash "$HOOK" 2>/dev/null)
code=$?
if [[ "$code" == "0" && -z "$output" ]]; then
    pass "(g) empty transcript_path -> allow"
else
    fail "(g) empty transcript_path -> allow (got exit=$code output=$output)"
fi

# --- real "Agent" dispatch tool name detection (issue #5086) ----------------
# The harness names the async subagent-dispatch tool "Agent", not "Task" — the
# original `.name=="Task"`-only match never fired for a real dispatch. An
# `Agent` dispatch also gets an IMMEDIATE launch-ack tool_result on the SAME
# tool_use id the real completion later arrives on, so the fix must not treat
# that ack alone as resolution (the #4389 hazard recurring on this tool).

# (a5086) dispatched Agent with ONLY the launch ack (no later distinct
# completion, no TaskOutput poll) -> still block. This is the exact silent
# no-op / then-false-negative hazard #5086 was filed for.
T5086a="$TMPROOT/transcript-agent-ack-only.jsonl"
write_transcript "$T5086a" "$AGENT5086_USE_UNRESOLVED" "$AGENT5086_ACK_UNRESOLVED"
result=$(run_hook "$T5086a" false)
assert_block "(a5086) dispatched Agent with only the launch ack -> block" "$result"

# (b5086) dispatched Agent with a LATER, distinct completion tool_result on
# the SAME id -> resolves.
T5086b="$TMPROOT/transcript-agent-later-completion.jsonl"
write_transcript "$T5086b" "$AGENT5086_USE_COMPLETED" "$AGENT5086_ACK_COMPLETED" "$AGENT5086_RESULT_COMPLETED"
result=$(run_hook "$T5086b" false)
assert_allow "(b5086) dispatched Agent + later distinct completion tool_result -> allow" "$result"

# (c5086) dispatched Agent resolved ONLY via a blocking TaskOutput poll of the
# agentId recovered from the launch ack (no second tool_result on the dispatch
# id itself) -> resolves.
T5086c="$TMPROOT/transcript-agent-taskoutput-poll.jsonl"
write_transcript "$T5086c" "$AGENT5086_USE_POLLED" "$AGENT5086_ACK_POLLED" \
    "$AGENT5086_POLL_USE" "$AGENT5086_POLL_RESULT_TERMINAL"
result=$(run_hook "$T5086c" false)
assert_allow "(c5086) dispatched Agent resolved only via blocking TaskOutput poll -> allow" "$result"

# (c5086b) guard against overcorrecting: a TaskOutput poll that is still
# <status>running</status> (not terminal) must NOT resolve the dispatch.
T5086cb="$TMPROOT/transcript-agent-taskoutput-still-running.jsonl"
write_transcript "$T5086cb" "$AGENT5086_USE_POLLED" "$AGENT5086_ACK_POLLED" \
    "$AGENT5086_POLL_USE" "$AGENT5086_POLL_RESULT_RUNNING"
result=$(run_hook "$T5086cb" false)
assert_block "(c5086b) TaskOutput poll still <status>running</status> -> still block" "$result"

# (d5086) existing Task-named fixtures continue to pass unmodified
# (back-compat): the original unresolved/resolved Task cases from (a)/(b)
# above are re-asserted here under the new dual-name (Task|Agent) matcher to
# make the back-compat guarantee an explicit, standalone regression case.
result=$(run_hook "$T1" false)
assert_block "(d5086) back-compat: unresolved Task-named dispatch -> still block" "$result"
result=$(run_hook "$T2" false)
assert_allow "(d5086) back-compat: resolved Task-named dispatch -> still allow" "$result"

# --- background Bash (run_in_background) detection (issue #4389) -----------

# (h) outstanding background Bash task (dispatch + immediate ack only, no
# later task-notification) -> block once. Structural-verification case from
# the issue: a synthetic transcript with an outstanding background Bash task
# fed to the hook via a Stop payload must produce {"decision":"block"}.
T5="$TMPROOT/transcript-bg-outstanding.jsonl"
write_transcript "$T5" "$BG_USE_UNRESOLVED" "$BG_ACK_UNRESOLVED"
result=$(run_hook "$T5" false)
assert_block "(h) outstanding background Bash task -> block" "$result"

# (i) completed background Bash task, notification in the REAL `queue-operation`
# shape (top-level .content string echoing the dispatch tool-use-id) -> allow.
# This is the shape the harness actually emits (#4482); the old fixture used a
# synthetic `type=="user"` string-content entry that never occurs live and so
# never exercised the matcher against reality.
T6="$TMPROOT/transcript-bg-completed-queueop.jsonl"
write_transcript "$T6" "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_QUEUEOP"
result=$(run_hook "$T6" false)
assert_allow "(i) completed background Bash task (queue-operation shape) -> allow" "$result"

# (i2) completed background Bash task, notification in the REAL `attachment`
# shape (.attachment.prompt string) -> allow. The harness may emit either or
# both shapes for the same event, so the matcher must recognize each.
T6b="$TMPROOT/transcript-bg-completed-attachment.jsonl"
write_transcript "$T6b" "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_ATTACHMENT"
result=$(run_hook "$T6b" false)
assert_allow "(i2) completed background Bash task (attachment shape) -> allow" "$result"

# (i3) both real shapes present for the same event (as live transcripts show)
# -> still allow (no double-count / no interference).
T6c="$TMPROOT/transcript-bg-completed-both.jsonl"
write_transcript "$T6c" "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_QUEUEOP" "$BG_NOTIFICATION_ATTACHMENT"
result=$(run_hook "$T6c" false)
assert_allow "(i3) completed background Bash task (both shapes) -> allow" "$result"

# (i4) multi-stop-sequence: a second stop over the SAME completed-and-notified
# transcript also allows. The guard re-scans the full transcript fresh on every
# Stop and the notification event does not disappear, so correct matching must
# hold on every scan — not just the "first" one (#4482 regression guard).
result=$(run_hook "$T6" false)
assert_allow "(i4) completed background Bash task -> allow again on second stop sequence" "$result"

# (j) outstanding background Bash task + stop_hook_active=true -> allow
# unconditionally (loop guard applies identically to the bg-Bash detector)
result=$(run_hook "$T5" true)
assert_allow "(j) outstanding background Bash task + stop_hook_active=true -> allow (loop guard)" "$result"

# (k) block reason for an outstanding background Bash task mentions the
# run_in_background hazard (not just the Task-subagent wording)
raw_bg=$(run_hook "$T5" false)
out_bg="${raw_bg#*|}"
reason_bg=$(echo "$out_bg" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_bg" == *"background Bash"* && "$reason_bg" == *"#4389"* ]]; then
    pass "(k) block reason explains the background-Bash hazard"
else
    fail "(k) block reason explains the background-Bash hazard (got: $reason_bg)"
fi

# --- armed Monitor / ScheduleWakeup detection (issue #4462) -----------------

# (l) armed-but-unfired Monitor (dispatch + immediate ack only, no later
# task-notification) -> block once. This is the exact #4462 transport-failure
# strand: a Monitor {command: "sleep 90 && …"} armed and then the turn ended.
T7="$TMPROOT/transcript-monitor-armed.jsonl"
write_transcript "$T7" "$MON_USE_UNRESOLVED" "$MON_ACK_UNRESOLVED"
result=$(run_hook "$T7" false)
assert_block "(l) armed-but-unfired Monitor -> block" "$result"

# (m) fired Monitor -> allow. The fire event resolves the armed timer. Uses the
# REAL notification shape (<task-id> only, NO <tool-use-id>) per #4696.
T8="$TMPROOT/transcript-monitor-fired.jsonl"
write_transcript "$T8" "$MON_USE_RESOLVED" "$MON_ACK_RESOLVED" "$MON_NOTIFICATION_QUEUEOP"
result=$(run_hook "$T8" false)
assert_allow "(m) fired Monitor (task-notification, task-id shape) -> allow" "$result"

# (m2) fired Monitor, notification in the `attachment` shape -> allow.
T8b="$TMPROOT/transcript-monitor-fired-attachment.jsonl"
write_transcript "$T8b" "$MON_USE_RESOLVED" "$MON_ACK_RESOLVED" "$MON_NOTIFICATION_ATTACHMENT"
result=$(run_hook "$T8b" false)
assert_allow "(m2) fired Monitor (attachment shape) -> allow" "$result"

# (n) armed ScheduleWakeup (sibling tool name, no fire event) -> block. The
# same detector must catch both tool names.
T9="$TMPROOT/transcript-wakeup-armed.jsonl"
write_transcript "$T9" "$WAKE_USE_UNRESOLVED"
result=$(run_hook "$T9" false)
assert_block "(n) armed ScheduleWakeup -> block" "$result"

# (o) armed Monitor + stop_hook_active=true -> allow unconditionally (loop
# guard applies identically to the Monitor detector)
result=$(run_hook "$T7" true)
assert_allow "(o) armed Monitor + stop_hook_active=true -> allow (loop guard)" "$result"

# (p) block reason for an armed Monitor mentions the #4462 transport-backoff
# hazard (not just the Task-subagent / background-Bash wording)
raw_mon=$(run_hook "$T7" false)
out_mon="${raw_mon#*|}"
reason_mon=$(echo "$out_mon" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_mon" == *"Monitor"* && "$reason_mon" == *"#4462"* ]]; then
    pass "(p) block reason explains the armed-Monitor #4462 hazard"
else
    fail "(p) block reason explains the armed-Monitor #4462 hazard (got: $reason_mon)"
fi

# --- Monitor resolution shapes (issue #4696) --------------------------------
# Third transcript-format gap after #4482/#4462: a Monitor's fired-event
# <task-notification> carries ONLY <task-id>, never <tool-use-id>, so matching
# Monitor dispatch ids against <tool-use-id> could NEVER observe a resolution —
# every Monitor ever armed re-blocked one stop per stop sequence for the rest of
# the session. Resolution is now keyed on the TASK id recovered from the ack.

# (q) armed + fired + explicitly TaskStop'd -> allow. The exact observed
# false-positive sequence from #4696.
T10="$TMPROOT/transcript-monitor-taskstopped.jsonl"
write_transcript "$T10" "$MON_USE_RESOLVED" "$MON_ACK_RESOLVED" "$MON_NOTIFICATION_QUEUEOP" \
    "$MON_TASKSTOP_USE" "$MON_TASKSTOP_RESULT"
result=$(run_hook "$T10" false)
assert_allow "(q) armed + fired + TaskStop'd Monitor -> allow" "$result"

# (q2) armed + TaskStop'd, with NO fired event and NO elapsed timeout -> allow.
# TaskStop alone must retire a persistent timer (which has no self-timeout).
T11="$TMPROOT/transcript-monitor-taskstop-only.jsonl"
write_transcript "$T11" "$MON_USE_UNRESOLVED" "$MON_ACK_UNRESOLVED" \
    "$MON_TASKSTOP_USE_01" "$MON_TASKSTOP_RESULT_01"
result=$(run_hook "$T11" false)
assert_allow "(q2) armed + TaskStop'd Monitor (no fire, no timeout) -> allow" "$result"

# (q3) armed + fired + its own configured timeout long elapsed -> allow. An
# expired timer cannot fire again, so it cannot orphan anything.
T12="$TMPROOT/transcript-monitor-timed-out.jsonl"
write_transcript "$T12" "$MON_USE_TIMEOUT" "$MON_ACK_TIMEOUT" "$MON_NOTIFICATION_TIMEOUT"
result=$(run_hook "$T12" false)
assert_allow "(q3) armed + fired + timed-out Monitor -> allow" "$result"

# (q4) armed with a timeout, never fired, timeout long elapsed -> allow.
T13="$TMPROOT/transcript-monitor-expired-silent.jsonl"
write_transcript "$T13" "$MON_USE_TIMEOUT" "$MON_ACK_TIMEOUT"
result=$(run_hook "$T13" false)
assert_allow "(q4) armed Monitor whose timeout has elapsed (never fired) -> allow" "$result"

# (q5) armed with a timeout that has NOT yet elapsed -> STILL block. Guards
# against overcorrecting the timeout rule into a blanket allow.
NOW_TS="$(date -u +%Y-%m-%dT%H:%M:%S).000Z"
MON_USE_FRESH="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_mon05\",\"name\":\"Monitor\",\"input\":{\"command\":\"sleep 90\",\"timeout_ms\":3600000}}]}}"
MON_ACK_FRESH="{\"type\":\"user\",\"timestamp\":\"$NOW_TS\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_mon05\",\"content\":\"Monitor started (task bmon0005, timeout 3600000ms). You will be notified on each event.\"}]}}"
T14="$TMPROOT/transcript-monitor-fresh.jsonl"
write_transcript "$T14" "$MON_USE_FRESH" "$MON_ACK_FRESH"
result=$(run_hook "$T14" false)
assert_block "(q5) armed Monitor whose timeout has NOT elapsed -> block" "$result"

# (q6) genuinely armed persistent Monitor (no TaskStop, no fire, no timeout) ->
# STILL block. The core regression guard: the #4462 detection must survive.
result=$(run_hook "$T7" false)
assert_block "(q6) genuinely armed Monitor (no resolution signal) -> still block" "$result"

# (q7) a retired timer is never re-flagged on a LATER stop sequence in the same
# session. Resolution is derived from durable, append-only transcript facts, so
# re-scanning the same transcript must keep allowing.
result=$(run_hook "$T10" false)
assert_allow "(q7) retired Monitor -> allow again on a second stop sequence" "$result"
result=$(run_hook "$T12" false)
assert_allow "(q7b) timed-out Monitor -> allow again on a second stop sequence" "$result"

# (q8) a Monitor whose arming call ERRORED -> allow. No timer was ever created.
T15="$TMPROOT/transcript-monitor-arm-failed.jsonl"
write_transcript "$T15" "$MON_USE_ERRORED" "$MON_ACK_ERRORED"
result=$(run_hook "$T15" false)
assert_allow "(q8) Monitor whose arming call errored -> allow" "$result"

# (q9) mixed transcript: one retired timer + one genuinely armed timer -> block,
# and the block must count exactly ONE armed timer (no over- or under-counting).
T16="$TMPROOT/transcript-monitor-mixed.jsonl"
write_transcript "$T16" "$MON_USE_RESOLVED" "$MON_ACK_RESOLVED" "$MON_NOTIFICATION_QUEUEOP" \
    "$MON_TASKSTOP_USE" "$MON_TASKSTOP_RESULT" "$MON_USE_UNRESOLVED" "$MON_ACK_UNRESOLVED"
raw_mix=$(run_hook "$T16" false)
assert_block "(q9) mixed retired + armed Monitors -> block" "$raw_mix"
reason_mix=$(echo "${raw_mix#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_mix" == *"1 armed Monitor/ScheduleWakeup timer(s)"* ]]; then
    pass "(q9b) mixed transcript counts exactly 1 armed Monitor"
else
    fail "(q9b) mixed transcript counts exactly 1 armed Monitor (got: $reason_mix)"
fi

# --- ScheduleWakeup resolution shapes (issue #4696) -------------------------

# (s) armed ScheduleWakeup whose delay has long elapsed -> allow. A fired wakeup
# re-invokes the session and leaves NO task id and NO task-notification, so the
# elapsed delay is the only signal it ever produces. Without this, an overnight
# reschedule loop re-blocks one stop per stop sequence forever.
T18="$TMPROOT/transcript-wakeup-elapsed.jsonl"
write_transcript "$T18" "$WAKE_USE_ELAPSED" "$WAKE_ACK_ELAPSED"
result=$(run_hook "$T18" false)
assert_allow "(s) armed ScheduleWakeup whose delay has elapsed -> allow" "$result"

# (s2) armed ScheduleWakeup whose delay has NOT elapsed -> STILL block.
NOW_TS2="$(date -u +%Y-%m-%dT%H:%M:%S).000Z"
WAKE_USE_FRESH="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS2\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_wake05\",\"name\":\"ScheduleWakeup\",\"input\":{\"delaySeconds\":1500,\"prompt\":\"tick\"}}]}}"
WAKE_ACK_FRESH="{\"type\":\"user\",\"timestamp\":\"$NOW_TS2\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_wake05\",\"content\":\"Next wakeup scheduled for 23:59:00 (in 1500s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives.\"}]}}"
T19="$TMPROOT/transcript-wakeup-fresh.jsonl"
write_transcript "$T19" "$WAKE_USE_FRESH" "$WAKE_ACK_FRESH"
result=$(run_hook "$T19" false)
assert_block "(s2) armed ScheduleWakeup whose delay has NOT elapsed -> block" "$result"

# (s3) a later ScheduleWakeup {stop:true} cancel retires the pending wakeup, and
# the cancel call itself arms nothing.
T20="$TMPROOT/transcript-wakeup-cancelled.jsonl"
write_transcript "$T20" "$WAKE_USE_FRESH" "$WAKE_ACK_FRESH" "$WAKE_CANCEL_USE" "$WAKE_CANCEL_RESULT"
result=$(run_hook "$T20" false)
assert_allow "(s3) ScheduleWakeup {stop:true} cancel -> allow" "$result"

# (s4) a ScheduleWakeup whose arming call errored -> allow. Nothing was armed.
T21="$TMPROOT/transcript-wakeup-arm-failed.jsonl"
write_transcript "$T21" "$WAKE_USE_ERRORED" "$WAKE_ACK_ERRORED"
result=$(run_hook "$T21" false)
assert_allow "(s4) ScheduleWakeup whose arming call errored -> allow" "$result"

# (t) background Bash explicitly TaskStop'd instead of completing -> allow. The
# same false-positive class as the Monitor gap: a stopped task cannot be
# orphaned by ending the turn (#4696).
T17="$TMPROOT/transcript-bg-taskstopped.jsonl"
write_transcript "$T17" "$BG_USE_STOPPED" "$BG_ACK_STOPPED" "$BG_TASKSTOP_USE" "$BG_TASKSTOP_RESULT"
result=$(run_hook "$T17" false)
assert_allow "(t) TaskStop'd background Bash task -> allow" "$result"

# --- background Bash completion shapes the pre-#5013 matcher missed ----------
# The constant "1 outstanding" false positive: a background Bash task whose
# completion never carried a `<tool-use-id>` echo re-blocked one stop per stop
# sequence for the whole session, even on turns that dispatched no new work.

# (u1) completion notification carries ONLY <task-id> (Monitor-shaped) -> allow.
# Keyed on the TASK id recovered from the dispatch ack, not the tool-use id.
T22="$TMPROOT/transcript-bg-taskid-only.jsonl"
write_transcript "$T22" "$BG_USE_TASKID_ONLY" "$BG_ACK_TASKID_ONLY" "$BG_NOTIFICATION_TASKID_ONLY"
result=$(run_hook "$T22" false)
assert_allow "(u1) background Bash notified by <task-id> only -> allow" "$result"

# (u2) awaited via a blocking BashOutput read (non-error result), NO
# task-notification at all -> allow. A blocking read in headless mode returns
# only after the task completed and can itself consume the notification.
T23="$TMPROOT/transcript-bg-blocking-read.jsonl"
write_transcript "$T23" "$BG_USE_READ" "$BG_ACK_READ" "$BG_READ_USE" "$BG_READ_RESULT"
result=$(run_hook "$T23" false)
assert_allow "(u2) background Bash awaited via blocking BashOutput -> allow" "$result"

# (u3) full acceptance-criteria regression scenario: N=2 completed+notified
# background Bash tasks + M=2 async agents (Task, ack-resolved, awaited via a
# blocking TaskOutput) + a foreground-only final turn -> NOT blocked. Async
# agents are never counted as background Bash tasks.
T24="$TMPROOT/transcript-bg-mixed-clean.jsonl"
write_transcript "$T24" \
    "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_QUEUEOP" \
    "$BG_USE_TASKID_ONLY" "$BG_ACK_TASKID_ONLY" "$BG_NOTIFICATION_TASKID_ONLY" \
    "$AGENT_USE" "$AGENT_ACK" "$AGENT_READ_USE" "$AGENT_READ_RESULT" \
    "$FG_FINAL_TURN" "$FG_FINAL_RESULT"
result=$(run_hook "$T24" false)
assert_allow "(u3) N completed bg tasks + M async agents (TaskOutput) + fg turn -> allow" "$result"

# (u3b) same scenario re-scanned on a second stop sequence -> still allow. The
# false positive in the field re-fired on EVERY stop, so resolution must be
# derived from durable transcript facts, not one-shot state.
result=$(run_hook "$T24" false)
assert_allow "(u3b) mixed clean transcript -> allow again on second stop sequence" "$result"

# (u4) true-positive retained: a genuinely running background Bash task (no
# notification of any shape, no blocking read, no TaskStop) STILL blocks the
# first stop, even amid the resolved tasks and async agents of (u3).
T25="$TMPROOT/transcript-bg-genuine-running.jsonl"
write_transcript "$T25" \
    "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_QUEUEOP" \
    "$AGENT_USE" "$AGENT_ACK" "$AGENT_READ_USE" "$AGENT_READ_RESULT" \
    "$BG_USE_UNRESOLVED" "$BG_ACK_UNRESOLVED"
result=$(run_hook "$T25" false)
assert_block "(u4) genuinely running background Bash task amid resolved work -> block" "$result"
reason_u4=$(echo "${result#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_u4" == *"1 background Bash"* ]]; then
    pass "(u4b) block counts exactly the 1 genuinely-running background Bash task"
else
    fail "(u4b) block counts exactly the 1 genuinely-running background Bash task (got: $reason_u4)"
fi

# --- block reason mentions the #3822/#4257 hazard ---------------------------
raw=$(run_hook "$T1" false)
out="${raw#*|}"
reason=$(echo "$out" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason" == *"claude -p"* && "$reason" == *"#3822"* ]]; then
    pass "block reason explains the headless -p kill-signal hazard"
else
    fail "block reason explains the headless -p kill-signal hazard (got: $reason)"
fi

# --- contract: block output is valid JSON -----------------------------------
if echo "$out" | jq empty 2>/dev/null; then
    pass "contract: block output is valid JSON"
else
    fail "contract: block output is valid JSON (got: $out)"
fi

# --- guards.backgroundSubagents config toggle -> disables the guard --------
mkdir -p "$TMPROOT/.loom"
cat > "$TMPROOT/.loom/config.json" <<'EOF'
{"guards": {"backgroundSubagents": false}}
EOF
result=$(run_hook "$T1" false)
assert_allow "guards.backgroundSubagents:false in config -> allow" "$result"
rm -f "$TMPROOT/.loom/config.json"

# --- LOOM_GUARD_BACKGROUND_SUBAGENTS=0 env override -> disables the guard --
result=$(run_hook "$T1" false LOOM_GUARD_BACKGROUND_SUBAGENTS=0)
assert_allow "LOOM_GUARD_BACKGROUND_SUBAGENTS=0 -> allow" "$result"

# --- env overrides config: env=1 forces guard on even if config says false -
cat > "$TMPROOT/.loom/config.json" <<'EOF'
{"guards": {"backgroundSubagents": false}}
EOF
result=$(run_hook "$T1" false LOOM_GUARD_BACKGROUND_SUBAGENTS=1)
assert_block "LOOM_GUARD_BACKGROUND_SUBAGENTS=1 overrides config:false -> block" "$result"
rm -f "$TMPROOT/.loom/config.json"

# --- jq absent -> allow (fail-open) -----------------------------------------
NOJQ_DIR="$(mktemp -d)"
for b in bash cat mkdir echo date sed dirname basename find python3 grep git env; do
    p=$(command -v "$b" 2>/dev/null) && ln -sf "$p" "$NOJQ_DIR/$b"
done
out=$(cd "$TMPROOT" && printf '{"transcript_path":"%s","stop_hook_active":false}' "$T1" | PATH="$NOJQ_DIR" bash "$HOOK" 2>/dev/null)
code=$?
if [[ "$code" == "0" && -z "$out" ]]; then
    pass "jq absent -> allow (fail-open)"
else
    fail "jq absent -> allow (fail-open) (got exit=$code output=$out)"
fi
rm -rf "$NOJQ_DIR"

echo "=== $PASS/$TOTAL passed ==="
[[ "$FAIL" -eq 0 ]]
