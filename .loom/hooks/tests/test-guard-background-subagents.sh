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
#   - synchronous Agent/Task dispatch resolution (#5243): a
#     `run_in_background: false` dispatch blocks the turn, so its first and only
#     tool_result is the real final result -- any tool_result on the id resolves
#     it, even when the text incidentally carries "Async agent launched
#     successfully" boilerplate; an ASYNC dispatch with only its launch ack still
#     blocks (the short-circuit must not leak to async ids); a mixed sync/async
#     transcript with all dispatches completed allows the stop on the first try
#   - <task-notification> resolution for Agent dispatches (#5713): a
#     correctly-awaited async Agent dispatch's completion arrives ONLY as a
#     <task-notification> -- never a second, distinct tool_result -- so pattern 1
#     must accept it, the same evidence pattern 2 (background Bash) already
#     accepts; resolves via EITHER a <tool-use-id> match on the dispatch id OR a
#     <task-id> match on the agentId recovered from the launch ack; a genuinely
#     unresolved dispatch (no notification, no distinct tool_result, no terminal
#     TaskOutput poll) still blocks; a session dispatching N agents and observing
#     N completion notifications stops cleanly on the first attempt
#   - jq absent -> allow (fail-open)
#   - contract: block output is valid JSON with decision=="block" and a
#     non-empty reason; exit code is always 0
#   - block reason names a context-safe await recipe for local_agent
#     Task/Agent subagents (issue #6168): a bounded, NON-BLOCKING `TaskOutput`
#     poll (`block: false`) -- not a flat "blocking TaskOutput" instruction,
#     which can return a raw JSONL transcript dump on timeout instead of just
#     status -- and (issue #6645) it no longer prescribes "just end the turn",
#     the action the guard itself used to block
#   - session-mode detection (issue #6645): the SAME transcript with one
#     genuinely unresolved dispatch must BLOCK in headless mode and be ALLOWED
#     (a `continue: true` + `systemMessage` advisory, never a `decision:
#     block`) in interactive mode. Every resolution step is covered:
#     `LOOM_SESSION_MODE` override, the `LOOM_HEADLESS_SESSION` marker
#     spawn-claude.sh exports for print-mode spawns, an `sdk*`
#     `CLAUDE_CODE_ENTRYPOINT`, and a real argv probe of the owning `claude`
#     process (two live placeholder processes named `claude`, one with `-p`
#     and one without, pointed at via `CLAUDE_PID`). The FAIL-CLOSED default
#     is asserted explicitly -- an unresolvable `CLAUDE_PID`, a dead pid, and
#     an unrecognized `LOOM_SESSION_MODE` value all BLOCK -- because a
#     false "interactive" reading silently reintroduces the #4257 hazard.
#     The `stop_hook_active` loop guard is asserted to still short-circuit
#     ahead of the new branch in BOTH modes (allow, silent, no advisory), and
#     a fully-resolved transcript stays silent in interactive mode too
#   - /loop dynamic-mode continuation exemption (issue #6175): an armed
#     ScheduleWakeup whose prompt starts with "/loop" (with or without
#     arguments) or carries the "<<autonomous-loop-dynamic>>" sentinel is
#     recognized as an intentional loop re-entry and excluded entirely from
#     the outstanding-timer count -- a session whose only armed timer is a
#     recognized loop continuation allows the stop with no block at all. A
#     ScheduleWakeup whose prompt merely mentions "/loop" mid-text (not a
#     recognized prefix/sentinel) still blocks, and a Monitor is never
#     exempted regardless of its input fields (recognition is scoped to
#     ScheduleWakeup only). A mixed transcript (one orphaned Monitor + one
#     recognized loop-continuation ScheduleWakeup) still blocks on the
#     orphaned Monitor, counts exactly one outstanding timer, and the block
#     reason names the loop-continuation timer separately as
#     recognized/allowed/not-counted so the transcript reads unambiguously.
#
# The hook under test is the canonical source at defaults/ (the version-
# controlled source of truth), copied into an isolated temp git tree so the
# hook's MAIN_ROOT (used only for the config-toggle lookup) resolves there.
# Exit 0 = all pass, 1 = fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
# Prefer the installed hook (a Loom-installed consumer repo has no defaults/
# directory at all); fall back to defaults/ for Loom's own source tree. See
# issue #6496.
SRC_HOOK="$REPO_ROOT/.loom/hooks/guard-background-subagents.sh"
[[ -r "$SRC_HOOK" ]] || SRC_HOOK="$REPO_ROOT/defaults/hooks/guard-background-subagents.sh"

PASS=0
FAIL=0
TOTAL=0

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

TMPROOT="$(mktemp -d)"
FAKE_PIDS=()
cleanup() {
    local p
    for p in ${FAKE_PIDS[@]+"${FAKE_PIDS[@]}"}; do
        kill "$p" 2>/dev/null || true
    done
    rm -rf "$TMPROOT"
}
trap cleanup EXIT
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

# --- <task-notification> resolution for Agent dispatches (issue #5713) -------
# The only completion signal a correctly-awaited ASYNC Agent dispatch actually
# produces in this harness is a <task-notification> — never a second, distinct
# tool_result on the dispatch id. Pattern 2 (background Bash) already accepted
# this evidence; pattern 1 did not, so a correctly-awaited agent could never
# resolve and the unresolved count only grew across a session.

# (d5713a) dispatched Agent resolved by a <task-notification> whose
# <tool-use-id> echoes the dispatch id (the <task-id> is deliberately a
# DIFFERENT value here, to isolate the tool-use-id matching branch).
AGENT5713_USE_TOOLID='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent04","name":"Agent","input":{"prompt":"implement issue #4"}}]}}'
AGENT5713_ACK_TOOLID='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent04","content":"Async agent launched successfully. agentId: aagent04. Use SendMessage with to: aagent04 to communicate. You will be notified automatically when it completes."}]}}'
AGENT5713_NOTIFICATION_TOOLID='{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>unrelated999</task-id>\n<tool-use-id>toolu_agent04</tool-use-id>\n<status>completed</status>\n<result>Work complete: opened PR #7001 for issue #4.</result>\n</task-notification>"}'

# (d5713b) dispatched Agent resolved by a <task-notification> whose <task-id>
# equals the agentId recovered from the launch ack — the notification carries
# ONLY <task-id>, no <tool-use-id> at all (the shape some completions use).
AGENT5713_USE_TASKID='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent05","name":"Agent","input":{"prompt":"implement issue #5"}}]}}'
AGENT5713_ACK_TASKID='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent05","content":"Async agent launched successfully. agentId: aagent05. Use SendMessage with to: aagent05 to communicate. You will be notified automatically when it completes."}]}}'
AGENT5713_NOTIFICATION_TASKID='{"type":"queue-operation","operation":"enqueue","content":"<task-notification>\n<task-id>aagent05</task-id>\n<status>completed</status>\n<result>Work complete: opened PR #7002 for issue #5.</result>\n</task-notification>"}'

# (d5713c) genuinely unresolved: dispatched Agent with only its launch ack, no
# task-notification, no distinct tool_result, no terminal TaskOutput poll ->
# must still block (proves the fix does not simply disable pattern 1).
AGENT5713_USE_ORPHAN='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_agent06","name":"Agent","input":{"prompt":"implement issue #6"}}]}}'
AGENT5713_ACK_ORPHAN='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_agent06","content":"Async agent launched successfully. agentId: aagent06. Use SendMessage with to: aagent06 to communicate. You will be notified automatically when it completes."}]}}'

# --- synchronous Agent/Task dispatch fixtures (issue #5243) -------------------
# A `run_in_background: false` dispatch is a STRUCTURAL guarantee: the harness
# cannot advance the turn (or reach a Stop) past a blocking tool_use until its
# result has landed, so the dispatch's first and only tool_result is always the
# real, final result — never a launch ack. Any tool_result on the id resolves it.

# (s5243) sync Agent whose only tool_result is the inline final result — no
# separate launch ack — resolves.
AGENT5243_SYNC_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_sync01","name":"Agent","input":{"prompt":"do the thing","run_in_background":false}}]}}'
AGENT5243_SYNC_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_sync01","content":"Work complete: opened PR #6001 for issue #10."}]}}'

# (s5243 edge) sync dispatch whose inline result text INCIDENTALLY contains the
# "Async agent launched successfully" boilerplate (e.g. shared harness ack
# wording) — must STILL resolve, because the launch-ack text exclusion is skipped
# for sync ids. This is the exact false-positive #5243 was filed for.
AGENT5243_SYNC_ACKTEXT_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_sync02","name":"Agent","input":{"prompt":"do the other thing","run_in_background":false}}]}}'
AGENT5243_SYNC_ACKTEXT_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_sync02","content":"Async agent launched successfully. agentId: async99. Work complete: PR #6002 opened."}]}}'

# (s5243 contrast) an ASYNC dispatch (run_in_background:true) with ONLY its launch
# ack must STILL block — the sync short-circuit must NOT leak to async ids.
AGENT5243_ASYNC_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_async01","name":"Agent","input":{"prompt":"async work","run_in_background":true}}]}}'
AGENT5243_ASYNC_ACK='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_async01","content":"Async agent launched successfully. agentId: async01. You will be notified automatically when it completes."}]}}'
# Sync Task (back-compat tool name) with run_in_background:false and inline result.
TASK5243_SYNC_USE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_sync03","name":"Task","input":{"subagent_type":"loom-builder","run_in_background":false}}]}}'
TASK5243_SYNC_RESULT='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_sync03","content":"Subagent completed: opened PR #6003."}]}}'

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

# --- issue #5976 fixtures: a background dispatch that NEVER STARTED ----------
# Reproduced byte-for-byte from the interactive session behind #5976 (a
# kicad-tools transcript, 2026-08-10): a `run_in_background: true` Bash
# dispatch DENIED by a PreToolUse guard hook. The tool never ran, so no
# background task, no task id, and no completion notification will EVER exist
# for it — yet the pre-#5976 detector counted it as outstanding on every stop
# for the rest of the session (the reported "1 background Bash command(s) ...
# have no completion notification" false positive, with `pgrep` showing no
# live process). The Monitor detector has always retired a failed arming call
# for exactly this reason ("d. The arming call itself erroring: no timer
# exists"); the background-Bash detector never consulted the ack's error flag.
#
# (v1) the real shape: `is_error: true` with a string body (guard denial).
BG_USE_DENIED='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg20","name":"Bash","input":{"command":"S=/tmp/scratch; uv run kct route board.kicad_pcb > \"$S/b03-route.log\" 2>&1","run_in_background":true}}]}}'
BG_ACK_DENIED='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg20","is_error":true,"content":"BLOCKED: Bash-tool write target '"'"'\"$S/b03-route.log\"'"'"' is an unexpanded shell variable from the path root down, so this guard cannot tell where the write lands. Unresolvable write targets fail closed (#4921). (#4178)"}]}}'

# (v2) the other error encoding `results.err` recognizes: no `is_error` flag,
# but a `<tool_use_error>` envelope in the body (harness input-validation
# rejection of the dispatch itself).
BG_USE_DENIED_ENVELOPE='{"type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"toolu_bg21","name":"Bash","input":{"command":"sleep 100","run_in_background":true}}]}}'
BG_ACK_DENIED_ENVELOPE='{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"toolu_bg21","content":"<tool_use_error>InputValidationError: Bash failed due to the following issue: an unexpected parameter was provided</tool_use_error>"}]}}'

# Build stdin JSON. Args: <transcript_path> <stop_hook_active>
make_input() {
    local transcript="$1" active="${2:-false}"
    jq -n --arg tp "$transcript" --argjson active "$active" \
        '{session_id: "test", transcript_path: $tp, stop_hook_active: $active, hook_event_name: "Stop"}'
}

# Session-mode signals the hook reads (issue #6645). The suite itself usually
# runs INSIDE a Claude session, so the ambient environment carries real
# CLAUDE_PID / CLAUDE_CODE_ENTRYPOINT / LOOM_* values that would otherwise leak
# into every fixture and make results depend on whether the suite was launched
# from an interactive or a headless session. Strip them all, then pin the mode
# explicitly per invocation.
HOOK_ENV_BASE=(-u LOOM_SESSION_MODE -u LOOM_HEADLESS_SESSION -u CLAUDE_CODE_ENTRYPOINT -u CLAUDE_PID)

# Default invocation: mode pinned HEADLESS, so every pre-#6645 expectation in
# this suite (block/allow) is asserted against the blocking branch exactly as
# before. A trailing `LOOM_SESSION_MODE=...` in "$@" wins (env applies
# assignments left to right).
run_hook() {
    local transcript="$1" active="${2:-false}"
    shift; [[ $# -gt 0 ]] && shift
    local exit_code=0 output
    output=$(cd "$TMPROOT" && env "${HOOK_ENV_BASE[@]}" LOOM_SESSION_MODE=headless "$@" bash "$HOOK" < <(make_input "$transcript" "$active") 2>/dev/null) || exit_code=$?
    printf '%s|%s' "$exit_code" "$output"
}

# Detection-path invocation: NO mode pin, so `loom_session_mode()` actually
# resolves the signals passed in "$@" (or falls through to its fail-closed
# default). Used only by the #6645 session-mode tests below.
run_hook_detect() {
    local transcript="$1" active="${2:-false}"
    shift; [[ $# -gt 0 ]] && shift
    local exit_code=0 output
    output=$(cd "$TMPROOT" && env "${HOOK_ENV_BASE[@]}" "$@" bash "$HOOK" < <(make_input "$transcript" "$active") 2>/dev/null) || exit_code=$?
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

# Interactive-mode outcome (issue #6645): the stop is ALLOWED (no
# `decision` field at all, so nothing blocks) and the hook emits at most a
# one-line `systemMessage` advisory. Anything containing `decision: block` or
# the "STOP BLOCKED" banner fails this assertion.
assert_advisory() {
    local desc="$1" result="$2"
    local code="${result%%|*}" out="${result#*|}"
    if [[ "$code" != "0" ]]; then
        fail "$desc (expected exit 0, got NONZERO exit=$code)"
        return
    fi
    local decision cont msg
    decision=$(echo "$out" | jq -r '.decision // empty' 2>/dev/null || true)
    cont=$(echo "$out" | jq -r '.continue // empty' 2>/dev/null || true)
    msg=$(echo "$out" | jq -r '.systemMessage // empty' 2>/dev/null || true)
    if [[ -n "$decision" ]]; then
        fail "$desc (expected NO decision field, got decision=$decision)"
    elif [[ "$cont" != "true" ]]; then
        fail "$desc (expected continue=true, got: $out)"
    elif [[ -z "$msg" ]]; then
        fail "$desc (expected a non-empty systemMessage, got: $out)"
    elif [[ "$msg" == *"STOP BLOCKED"* ]]; then
        fail "$desc (advisory must never say STOP BLOCKED, got: $msg)"
    else
        pass "$desc"
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

# (n5713i) per-dispatch matching must be per-ID, not "any evidence anywhere":
# one agent resolved by a TERMINAL TaskOutput poll of ITS OWN agentId, plus one
# genuinely orphaned dispatch -> STILL block. `$arr | index(.)` rebinds `.` to
# $arr, so the pre-#5721 lookup matched for any non-empty evidence list and
# resolved unrelated dispatches wholesale (the same defect the notification
# branches from #5713 had to avoid).
T5713i="$TMPROOT/transcript-agent-poll-plus-orphan.jsonl"
write_transcript "$T5713i" \
    "$AGENT5086_USE_POLLED" "$AGENT5086_ACK_POLLED" \
    "$AGENT5086_POLL_USE" "$AGENT5086_POLL_RESULT_TERMINAL" \
    "$AGENT5713_USE_ORPHAN" "$AGENT5713_ACK_ORPHAN"
raw_5713i=$(run_hook "$T5713i" false)
assert_block "(n5713i) terminal-polled agent + one orphan -> block" "$raw_5713i"
reason_5713i=$(echo "${raw_5713i#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_5713i" == *"1 dispatched Task/Agent"* ]]; then
    pass "(n5713j) polled+orphan transcript counts exactly 1 orphaned Task/Agent"
else
    fail "(n5713j) polled+orphan transcript counts exactly 1 orphaned Task/Agent (got: $reason_5713i)"
fi

# (d5086) existing Task-named fixtures continue to pass unmodified
# (back-compat): the original unresolved/resolved Task cases from (a)/(b)
# above are re-asserted here under the new dual-name (Task|Agent) matcher to
# make the back-compat guarantee an explicit, standalone regression case.
result=$(run_hook "$T1" false)
assert_block "(d5086) back-compat: unresolved Task-named dispatch -> still block" "$result"
result=$(run_hook "$T2" false)
assert_allow "(d5086) back-compat: resolved Task-named dispatch -> still allow" "$result"

# --- <task-notification> resolution for Agent dispatches (issue #5713) ------
# Pattern 1 must accept a <task-notification> as resolution -- the same
# evidence pattern 2 (background Bash) already accepts -- since it is the
# ONLY completion signal a correctly-awaited async Agent dispatch produces.

# (a5713) dispatched Agent resolved ONLY by a <task-notification> whose
# <tool-use-id> matches the dispatch id -> allow.
T5713a="$TMPROOT/transcript-agent-notif-toolid.jsonl"
write_transcript "$T5713a" "$AGENT5713_USE_TOOLID" "$AGENT5713_ACK_TOOLID" "$AGENT5713_NOTIFICATION_TOOLID"
result=$(run_hook "$T5713a" false)
assert_allow "(a5713) Agent dispatch resolved by <task-notification> tool-use-id match -> allow" "$result"

# (b5713) dispatched Agent resolved ONLY by a <task-notification> whose
# <task-id> equals the agentId recovered from the launch ack (no
# <tool-use-id> tag in the notification at all) -> allow.
T5713b="$TMPROOT/transcript-agent-notif-taskid.jsonl"
write_transcript "$T5713b" "$AGENT5713_USE_TASKID" "$AGENT5713_ACK_TASKID" "$AGENT5713_NOTIFICATION_TASKID"
result=$(run_hook "$T5713b" false)
assert_allow "(b5713) Agent dispatch resolved by <task-notification> task-id==agentId match -> allow" "$result"

# (c5713) genuinely unresolved Agent dispatch (launch ack only, no
# notification, no distinct tool_result, no terminal TaskOutput poll) ->
# STILL block. Proves the fix does not simply disable pattern 1.
T5713c="$TMPROOT/transcript-agent-orphan.jsonl"
write_transcript "$T5713c" "$AGENT5713_USE_ORPHAN" "$AGENT5713_ACK_ORPHAN"
result=$(run_hook "$T5713c" false)
assert_block "(c5713) genuinely unresolved Agent dispatch (no notification) -> still block" "$result"

# (d5713) acceptance-criteria regression: a session that dispatches N=3 agents
# (one resolved via a distinct tool_result, one via tool-use-id notification,
# one via task-id notification) and observes completion for all of them stops
# cleanly on the FIRST attempt.
T5713d="$TMPROOT/transcript-agent-n-notified.jsonl"
write_transcript "$T5713d" \
    "$AGENT5086_USE_COMPLETED" "$AGENT5086_ACK_COMPLETED" "$AGENT5086_RESULT_COMPLETED" \
    "$AGENT5713_USE_TOOLID" "$AGENT5713_ACK_TOOLID" "$AGENT5713_NOTIFICATION_TOOLID" \
    "$AGENT5713_USE_TASKID" "$AGENT5713_ACK_TASKID" "$AGENT5713_NOTIFICATION_TASKID"
result=$(run_hook "$T5713d" false)
assert_allow "(d5713) N dispatched agents + N completion notifications -> allow on first attempt" "$result"

# (e5713) mixed transcript: the same N=3 resolved agents from (d5713) PLUS one
# genuinely orphaned dispatch -> block, and the count is exactly 1 (no
# over-count of the resolved agents, no under-count of the orphan).
T5713e="$TMPROOT/transcript-agent-mixed-one-orphan.jsonl"
write_transcript "$T5713e" \
    "$AGENT5086_USE_COMPLETED" "$AGENT5086_ACK_COMPLETED" "$AGENT5086_RESULT_COMPLETED" \
    "$AGENT5713_USE_TOOLID" "$AGENT5713_ACK_TOOLID" "$AGENT5713_NOTIFICATION_TOOLID" \
    "$AGENT5713_USE_TASKID" "$AGENT5713_ACK_TASKID" "$AGENT5713_NOTIFICATION_TASKID" \
    "$AGENT5713_USE_ORPHAN" "$AGENT5713_ACK_ORPHAN"
raw_5713e=$(run_hook "$T5713e" false)
assert_block "(e5713) N resolved agents + 1 orphan -> block" "$raw_5713e"
reason_5713e=$(echo "${raw_5713e#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_5713e" == *"1 dispatched Task/Agent"* ]]; then
    pass "(f5713) mixed transcript counts exactly 1 orphaned Agent dispatch"
else
    fail "(f5713) mixed transcript counts exactly 1 orphaned Agent dispatch (got: $reason_5713e)"
fi

# (g5713) a resolved-by-notification transcript re-scanned on a second stop
# sequence also allows (no one-shot/stateful matching -- durable transcript
# facts stay resolved).
result=$(run_hook "$T5713a" false)
assert_allow "(g5713) Agent resolved by notification -> allow again on second stop sequence" "$result"

# --- synchronous Agent/Task dispatch resolution (issue #5243) ----------------
# A `run_in_background: false` dispatch blocks the turn, so its first and only
# tool_result is the real final result — never a launch ack. Any tool_result on
# the id resolves it, even when the text incidentally carries ack boilerplate.

# (s5243a) sync Agent whose only tool_result is the inline final result -> allow.
T5243a="$TMPROOT/transcript-agent-sync.jsonl"
write_transcript "$T5243a" "$AGENT5243_SYNC_USE" "$AGENT5243_SYNC_RESULT"
result=$(run_hook "$T5243a" false)
assert_allow "(s5243a) sync Agent (run_in_background:false) + inline result -> allow" "$result"

# (s5243b) sync Agent whose inline result text incidentally contains the
# "Async agent launched successfully" boilerplate -> STILL allow (the exact
# false positive #5243 was filed for; the launch-ack exclusion is skipped here).
T5243b="$TMPROOT/transcript-agent-sync-acktext.jsonl"
write_transcript "$T5243b" "$AGENT5243_SYNC_ACKTEXT_USE" "$AGENT5243_SYNC_ACKTEXT_RESULT"
result=$(run_hook "$T5243b" false)
assert_allow "(s5243b) sync Agent whose result text contains ack boilerplate -> allow" "$result"

# (s5243c) sync Task (back-compat tool name) with run_in_background:false -> allow.
T5243c="$TMPROOT/transcript-task-sync.jsonl"
write_transcript "$T5243c" "$TASK5243_SYNC_USE" "$TASK5243_SYNC_RESULT"
result=$(run_hook "$T5243c" false)
assert_allow "(s5243c) sync Task (run_in_background:false) + inline result -> allow" "$result"

# (s5243d) true-positive retained: an ASYNC dispatch (run_in_background:true) with
# ONLY its launch ack must STILL block. The sync short-circuit must not leak to
# async ids.
T5243d="$TMPROOT/transcript-agent-async-ack-only.jsonl"
write_transcript "$T5243d" "$AGENT5243_ASYNC_USE" "$AGENT5243_ASYNC_ACK"
result=$(run_hook "$T5243d" false)
assert_block "(s5243d) async Agent (run_in_background:true) with only launch ack -> block" "$result"

# (s5243e) acceptance-criterion regression fixture: mixed sync + async dispatches,
# ALL completed -> guard allows the stop on the first try. One sync Agent (inline
# result), one sync Task (inline result), one async Agent resolved by a later
# distinct completion tool_result, and one async Agent resolved via a blocking
# TaskOutput poll.
T5243e="$TMPROOT/transcript-mixed-sync-async-all-done.jsonl"
write_transcript "$T5243e" \
    "$AGENT5243_SYNC_USE" "$AGENT5243_SYNC_RESULT" \
    "$TASK5243_SYNC_USE" "$TASK5243_SYNC_RESULT" \
    "$AGENT5086_USE_COMPLETED" "$AGENT5086_ACK_COMPLETED" "$AGENT5086_RESULT_COMPLETED" \
    "$AGENT5086_USE_POLLED" "$AGENT5086_ACK_POLLED" "$AGENT5086_POLL_USE" "$AGENT5086_POLL_RESULT_TERMINAL"
result=$(run_hook "$T5243e" false)
assert_allow "(s5243e) mixed sync/async dispatches all completed -> allow first try" "$result"

# (s5243f) mixed transcript with one genuinely-orphaned async dispatch amid the
# resolved sync/async work -> STILL block exactly ONE Task/Agent (no under-count
# from the sync short-circuit, no over-count of the resolved ones).
T5243f="$TMPROOT/transcript-mixed-one-orphan.jsonl"
write_transcript "$T5243f" \
    "$AGENT5243_SYNC_USE" "$AGENT5243_SYNC_RESULT" \
    "$TASK5243_SYNC_USE" "$TASK5243_SYNC_RESULT" \
    "$AGENT5243_ASYNC_USE" "$AGENT5243_ASYNC_ACK"
raw_5243f=$(run_hook "$T5243f" false)
assert_block "(s5243f) mixed sync-done + one orphaned async -> block" "$raw_5243f"
reason_5243f=$(echo "${raw_5243f#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_5243f" == *"1 dispatched Task/Agent"* ]]; then
    pass "(s5243g) mixed transcript counts exactly 1 orphaned Task/Agent"
else
    fail "(s5243g) mixed transcript counts exactly 1 orphaned Task/Agent (got: $reason_5243f)"
fi

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

# --- /loop dynamic-mode continuation exemption (issue #6175) ----------------
# A `/loop`-style dynamic-mode session re-arms `ScheduleWakeup` on every
# iteration by design, precisely so it survives turn boundaries in an
# INTERACTIVE session -- ending the turn does not kill it, unlike the headless
# `-p` orphaning hazard this guard exists to catch. Blocking on the armed
# continuation timer is a false positive repeating once per loop iteration
# (15+ blocks/day observed). The guard recognizes a ScheduleWakeup whose
# `input.prompt` starts with `/loop` (optionally with arguments) or carries
# the `<<autonomous-loop-dynamic>>` sentinel, and excludes it entirely from
# the outstanding-timer count -- while a genuinely orphaned timer (a
# non-matching ScheduleWakeup, or any Monitor) still blocks.
NOW_TS6175="$(date -u +%Y-%m-%dT%H:%M:%S).000Z"

# (w6175a) armed ScheduleWakeup with prompt "/loop", still fresh (delay not
# elapsed) -> allow. The recognized loop re-entry is not counted at all, so a
# session whose ONLY armed timer is this one is allowed to stop with no block.
LOOP_USE_SLASH="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_loop01\",\"name\":\"ScheduleWakeup\",\"input\":{\"delaySeconds\":30,\"prompt\":\"/loop\"}}]}}"
LOOP_ACK_SLASH="{\"type\":\"user\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_loop01\",\"content\":\"Next wakeup scheduled for 23:59:30 (in 30s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives.\"}]}}"
T22L="$TMPROOT/transcript-loop-slash.jsonl"
write_transcript "$T22L" "$LOOP_USE_SLASH" "$LOOP_ACK_SLASH"
result=$(run_hook "$T22L" false)
assert_allow "(w6175a) armed ScheduleWakeup prompt '/loop' (fresh) -> allow" "$result"

# (w6175b) armed ScheduleWakeup carrying the `<<autonomous-loop-dynamic>>`
# sentinel instead of a literal `/loop` prefix -> allow.
LOOP_USE_SENTINEL="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_loop02\",\"name\":\"ScheduleWakeup\",\"input\":{\"delaySeconds\":30,\"prompt\":\"<<autonomous-loop-dynamic>>\"}}]}}"
LOOP_ACK_SENTINEL="{\"type\":\"user\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_loop02\",\"content\":\"Next wakeup scheduled for 23:59:30 (in 30s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives.\"}]}}"
T22M="$TMPROOT/transcript-loop-sentinel.jsonl"
write_transcript "$T22M" "$LOOP_USE_SENTINEL" "$LOOP_ACK_SENTINEL"
result=$(run_hook "$T22M" false)
assert_allow "(w6175b) armed ScheduleWakeup with <<autonomous-loop-dynamic>> sentinel -> allow" "$result"

# (w6175c) armed ScheduleWakeup whose prompt is "/loop" followed by arguments
# -> allow. The recognition must not require an exact, argument-free match.
LOOP_USE_ARGS="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_loop03\",\"name\":\"ScheduleWakeup\",\"input\":{\"delaySeconds\":30,\"prompt\":\"/loop --dynamic tick 3\"}}]}}"
LOOP_ACK_ARGS="{\"type\":\"user\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_loop03\",\"content\":\"Next wakeup scheduled for 23:59:30 (in 30s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives.\"}]}}"
T22N="$TMPROOT/transcript-loop-args.jsonl"
write_transcript "$T22N" "$LOOP_USE_ARGS" "$LOOP_ACK_ARGS"
result=$(run_hook "$T22N" false)
assert_allow "(w6175c) armed ScheduleWakeup prompt '/loop --dynamic tick 3' -> allow" "$result"

# (w6175d) armed ScheduleWakeup whose prompt merely MENTIONS "/loop" mid-text,
# not as a recognized re-entry -> STILL block. Regression guard: the
# recognition must stay narrow (prefix/sentinel only), not "any prompt
# containing the substring /loop".
LOOP_USE_MENTION="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_loop04\",\"name\":\"ScheduleWakeup\",\"input\":{\"delaySeconds\":30,\"prompt\":\"please check the /loop docs before the next tick\"}}]}}"
LOOP_ACK_MENTION="{\"type\":\"user\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_loop04\",\"content\":\"Next wakeup scheduled for 23:59:30 (in 30s). Nothing more to do this turn — the harness re-invokes you when the wakeup fires or a task-notification arrives.\"}]}}"
T22O="$TMPROOT/transcript-loop-mention.jsonl"
write_transcript "$T22O" "$LOOP_USE_MENTION" "$LOOP_ACK_MENTION"
result=$(run_hook "$T22O" false)
assert_block "(w6175d) ScheduleWakeup prompt merely mentioning /loop (not a re-entry) -> still block" "$result"

# (w6175e) a genuinely orphaned Monitor is NEVER exempted, even when its
# (nonstandard) input happens to carry a "/loop"-shaped `prompt` field --
# recognition is scoped to `ScheduleWakeup` only, `Monitor` has no prompt
# semantics at all.
MON_USE_LOOPLIKE="{\"type\":\"assistant\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_loop05\",\"name\":\"Monitor\",\"input\":{\"command\":\"sleep 90\",\"prompt\":\"/loop\",\"persistent\":true}}]}}"
MON_ACK_LOOPLIKE="{\"type\":\"user\",\"timestamp\":\"$NOW_TS6175\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_loop05\",\"content\":\"Monitor started (task bmonloop5, persistent — runs until TaskStop or session end). You will be notified on each event.\"}]}}"
T22P="$TMPROOT/transcript-monitor-looklike.jsonl"
write_transcript "$T22P" "$MON_USE_LOOPLIKE" "$MON_ACK_LOOPLIKE"
result=$(run_hook "$T22P" false)
assert_block "(w6175e) Monitor with a /loop-shaped 'prompt' field is NOT exempted -> still block" "$result"

# (w6175f) mixed transcript: one genuinely orphaned Monitor (blocks) + one
# recognized loop-continuation ScheduleWakeup (allowed) armed at the same
# time -> block (from the orphaned Monitor), the orphan count is exactly 1
# (the loop timer is not double-counted), and the reason distinguishes the
# two: the orphaned timer's sentence names it as blocking while a separate,
# clearly-labeled sentence names the loop timer as recognized/allowed/not
# counted -- so the transcript reads unambiguously.
T22Q="$TMPROOT/transcript-monitor-plus-loop.jsonl"
write_transcript "$T22Q" "$MON_USE_UNRESOLVED" "$MON_ACK_UNRESOLVED" "$LOOP_USE_SLASH" "$LOOP_ACK_SLASH"
raw_mix6175=$(run_hook "$T22Q" false)
assert_block "(w6175f) orphaned Monitor + loop-continuation ScheduleWakeup -> block" "$raw_mix6175"
reason_mix6175=$(echo "${raw_mix6175#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_mix6175" == *"1 armed Monitor/ScheduleWakeup timer(s)"* ]]; then
    pass "(w6175f2) mixed transcript counts exactly 1 orphaned timer (loop timer excluded)"
else
    fail "(w6175f2) mixed transcript counts exactly 1 orphaned timer (loop timer excluded) (got: $reason_mix6175)"
fi
if [[ "$reason_mix6175" == *"NOT blocking"* && "$reason_mix6175" == *"loop-continuation"* ]]; then
    pass "(w6175f3) reason names the loop-continuation timer separately as allowed/not blocking"
else
    fail "(w6175f3) reason names the loop-continuation timer separately as allowed/not blocking (got: $reason_mix6175)"
fi

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

# --- a background dispatch that never started (issue #5976) ------------------
# A `run_in_background: true` Bash dispatch whose tool_result is an ERROR (a
# PreToolUse guard denial, or a harness input-validation rejection) never
# created a background task: no task id was ever minted, so no completion
# notification, no blocking read and no TaskStop can ever exist for it. It must
# be retired exactly the way the Monitor detector has always retired a failed
# arming call — otherwise it is counted as outstanding on every stop for the
# rest of the session.

# (v5976a) guard-denied background dispatch (is_error:true) -> allow.
T26="$TMPROOT/transcript-bg-denied.jsonl"
write_transcript "$T26" "$BG_USE_DENIED" "$BG_ACK_DENIED"
result=$(run_hook "$T26" false)
assert_allow "(v5976a) guard-denied background Bash dispatch -> allow" "$result"

# (v5976b) same transcript, second stop sequence -> still allow. The field
# report was of a block that RECURRED on every stop; a durable transcript fact
# (the error ack) must hold on every scan, not just the first.
result=$(run_hook "$T26" false)
assert_allow "(v5976b) guard-denied background dispatch -> allow again on second stop sequence" "$result"

# (v5976c) <tool_use_error>-envelope rejection (no is_error flag) -> allow.
T27="$TMPROOT/transcript-bg-denied-envelope.jsonl"
write_transcript "$T27" "$BG_USE_DENIED_ENVELOPE" "$BG_ACK_DENIED_ENVELOPE"
result=$(run_hook "$T27" false)
assert_allow "(v5976c) <tool_use_error>-rejected background dispatch -> allow" "$result"

# (v5976d) the full reported session shape: a denied dispatch alongside a
# completed+notified background task, a completed Monitor and a completed
# Agent -> allow on the FIRST attempt (nothing is genuinely outstanding).
T28="$TMPROOT/transcript-5976-interactive.jsonl"
write_transcript "$T28" \
    "$BG_USE_DENIED" "$BG_ACK_DENIED" \
    "$BG_USE_RESOLVED" "$BG_ACK_RESOLVED" "$BG_NOTIFICATION_QUEUEOP" \
    "$MON_USE_RESOLVED" "$MON_ACK_RESOLVED" "$MON_NOTIFICATION_QUEUEOP" \
    "$AGENT5713_USE_TOOLID" "$AGENT5713_ACK_TOOLID" "$AGENT5713_NOTIFICATION_TOOLID" \
    "$FG_FINAL_TURN" "$FG_FINAL_RESULT"
result=$(run_hook "$T28" false)
assert_allow "(v5976d) denied dispatch + completed bg/Monitor/Agent -> allow" "$result"

# (v5976e) true positive retained: a denied dispatch does NOT mask a genuinely
# running background task in the same transcript, and the count is exactly 1.
T29="$TMPROOT/transcript-bg-denied-plus-running.jsonl"
write_transcript "$T29" "$BG_USE_DENIED" "$BG_ACK_DENIED" \
    "$BG_USE_UNRESOLVED" "$BG_ACK_UNRESOLVED"
result=$(run_hook "$T29" false)
assert_block "(v5976e) denied dispatch + genuinely running task -> block" "$result"
reason_v5=$(echo "${result#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_v5" == *"1 background Bash"* ]]; then
    pass "(v5976f) denied dispatch is not counted (exactly 1 outstanding)"
else
    fail "(v5976f) denied dispatch is not counted (exactly 1 outstanding) (got: $reason_v5)"
fi

# --- the block names WHICH ids it believes are outstanding (issue #5976) -----
# Without the ids, diagnosing a false positive means eliminating every dispatch
# in the session by hand. Each detector's clause must name its own ids.
if [[ "$reason_v5" == *"toolu_bg01"* && "$reason_v5" != *"toolu_bg20"* ]]; then
    pass "(w5976a) background-Bash block names the outstanding dispatch id"
else
    fail "(w5976a) background-Bash block names the outstanding dispatch id (got: $reason_v5)"
fi

raw_ids_task=$(run_hook "$T1" false)
reason_ids_task=$(echo "${raw_ids_task#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_ids_task" == *"toolu_01"* ]]; then
    pass "(w5976b) Task/Agent block names the unresolved dispatch id"
else
    fail "(w5976b) Task/Agent block names the unresolved dispatch id (got: $reason_ids_task)"
fi

raw_ids_mon=$(run_hook "$T7" false)
reason_ids_mon=$(echo "${raw_ids_mon#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_ids_mon" == *"toolu_mon01"* ]]; then
    pass "(w5976c) Monitor block names the still-armed timer id"
else
    fail "(w5976c) Monitor block names the still-armed timer id (got: $reason_ids_mon)"
fi

# (w5976d) the id list is bounded — a session with many outstanding dispatches
# must not emit an unbounded reason string.
T30="$TMPROOT/transcript-bg-many-outstanding.jsonl"
: > "$T30"
for i in 40 41 42 43 44 45 46 47 48 49 50 51; do
    printf '%s\n' "{\"type\":\"assistant\",\"message\":{\"role\":\"assistant\",\"content\":[{\"type\":\"tool_use\",\"id\":\"toolu_bg$i\",\"name\":\"Bash\",\"input\":{\"command\":\"sleep 100\",\"run_in_background\":true}}]}}" >> "$T30"
    printf '%s\n' "{\"type\":\"user\",\"message\":{\"role\":\"user\",\"content\":[{\"type\":\"tool_result\",\"tool_use_id\":\"toolu_bg$i\",\"content\":\"Command running in background with ID: bgmany$i. You will be notified when it completes.\"}]}}" >> "$T30"
done
raw_many=$(run_hook "$T30" false)
reason_many=$(echo "${raw_many#*|}" | jq -r '.reason // empty' 2>/dev/null || true)
if [[ "$reason_many" == *"12 background Bash"* && "$reason_many" == *"+4 more"* \
      && "$reason_many" == *"toolu_bg40"* && "$reason_many" != *"toolu_bg51"* ]]; then
    pass "(w5976d) outstanding-id list is truncated with a '+N more' suffix"
else
    fail "(w5976d) outstanding-id list is truncated with a '+N more' suffix (got: $reason_many)"
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

# --- block reason names a context-safe await recipe, not a blind blocking
# TaskOutput (issue #6168) -----------------------------------------------
# The block only ever fires in HEADLESS mode now (#6645), so the message names
# the headless recipe: a bounded, NON-BLOCKING TaskOutput poll -- not a flat
# "blocking TaskOutput", which can return a raw JSONL transcript dump.
if [[ "$reason" == *"#6168"* && "$reason" == *"HEADLESS"* \
      && "$reason" == *"NON-BLOCKING"* && "$reason" == *"block: false"* ]]; then
    pass "block reason names a context-safe await recipe (#6168)"
else
    fail "block reason names a context-safe await recipe (#6168) (got: $reason)"
fi

# --- block reason no longer prescribes the action it blocks (issue #6645) ---
# The pre-#6645 message told the orchestrator that in an INTERACTIVE session
# the correct recipe is to "just end the turn" -- and then blocked exactly
# that. The block is headless-only now, so that sentence must be gone.
if [[ "$reason" != *"just end the turn"* \
      && "$reason" != *"arrive on a later turn"* \
      && "$reason" == *"#6645"* ]]; then
    pass "(m6645-msg) block reason no longer prescribes the action it blocks"
else
    fail "(m6645-msg) block reason no longer prescribes the action it blocks (got: $reason)"
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

# =============================================================================
# Session-mode detection (issue #6645)
#
# The SAME transcript -- one genuinely unresolved dispatch, present in every
# case below -- must BLOCK in headless mode and be ALLOWED (advisory only) in
# interactive mode. Every case that cannot positively establish "interactive"
# must land on the blocking branch: that fail-closed default is the #4257
# safety floor and is asserted explicitly, not assumed.
# =============================================================================
echo "--- session-mode detection (#6645) ---"

# (m6645a) explicit interactive override -> allow with a systemMessage advisory
result=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=interactive)
assert_advisory "(m6645a) LOOM_SESSION_MODE=interactive + unresolved dispatch -> allow (advisory)" "$result"

# (m6645b) explicit headless override -> block, byte-for-byte the old behaviour
result=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=headless)
assert_block "(m6645b) LOOM_SESSION_MODE=headless + unresolved dispatch -> block" "$result"

# (m6645c) the two modes genuinely DIVERGE on identical input -- the core
# acceptance criterion, asserted as one comparison rather than two isolated
# expectations.
raw_int=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=interactive)
raw_head=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=headless)
if [[ "${raw_int#*|}" != "${raw_head#*|}" \
      && "${raw_head#*|}" == *"STOP BLOCKED"* \
      && "${raw_int#*|}" != *"STOP BLOCKED"* ]]; then
    pass "(m6645c) same unresolved transcript: headless blocks, interactive does not"
else
    fail "(m6645c) same unresolved transcript: headless blocks, interactive does not (interactive=${raw_int#*|} headless=${raw_head#*|})"
fi

# (m6645d) LOOM_HEADLESS_SESSION=1 (the marker spawn-claude.sh exports for every
# print-mode spawn) -> headless, with no LOOM_SESSION_MODE pin in play.
result=$(run_hook_detect "$T1" false LOOM_HEADLESS_SESSION=1)
assert_block "(m6645d) LOOM_HEADLESS_SESSION=1 -> block" "$result"

# (m6645e) an SDK entrypoint is programmatic, never a human at a terminal.
result=$(run_hook_detect "$T1" false CLAUDE_CODE_ENTRYPOINT=sdk-cli)
assert_block "(m6645e) CLAUDE_CODE_ENTRYPOINT=sdk-* -> block" "$result"

# (m6645f/g) argv probe of the owning `claude` process. Two REAL processes are
# spawned whose argv[0..1] basename is `claude` -- one carrying `-p` (print
# mode) and one not -- and CLAUDE_PID is pointed at each in turn. This
# exercises the actual /proc (or `ps`) read, not a stubbed-out branch.
mkdir -p "$TMPROOT/fakebin"
cat > "$TMPROOT/fakebin/claude" <<'EOF'
#!/bin/bash
sleep 60
EOF
chmod +x "$TMPROOT/fakebin/claude"

# stdout/stderr go to /dev/null so these placeholders never hold this suite's
# own output pipe open past the last assertion.
"$TMPROOT/fakebin/claude" -p "/loom:sweep 1" --dangerously-skip-permissions >/dev/null 2>&1 &
FAKE_PRINT_PID=$!
FAKE_PIDS+=("$FAKE_PRINT_PID")
"$TMPROOT/fakebin/claude" --dangerously-skip-permissions >/dev/null 2>&1 &
FAKE_TTY_PID=$!
FAKE_PIDS+=("$FAKE_TTY_PID")
# Let the kernel publish each child's argv before probing it.
for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -n "$(tr '\0' ' ' < "/proc/$FAKE_PRINT_PID/cmdline" 2>/dev/null || ps -o args= -p "$FAKE_PRINT_PID" 2>/dev/null)" ]] && break
    sleep 0.2
done

result=$(run_hook_detect "$T1" false "CLAUDE_PID=$FAKE_PRINT_PID")
assert_block "(m6645f) owning claude process argv carries -p -> block" "$result"

result=$(run_hook_detect "$T1" false "CLAUDE_PID=$FAKE_TTY_PID")
assert_advisory "(m6645g) owning claude process argv has no print flag -> allow (advisory)" "$result"

# (m6645h) FAIL-CLOSED: CLAUDE_PID is set but resolves to nothing we can call
# `claude`, so the session mode is undetermined -> block, never allow.
result=$(run_hook_detect "$T1" false "CLAUDE_PID=$$")
assert_block "(m6645h) CLAUDE_PID resolves to a non-claude process -> block (fail closed)" "$result"

result=$(run_hook_detect "$T1" false "CLAUDE_PID=2147483647")
assert_block "(m6645i) CLAUDE_PID names no live process -> block (fail closed)" "$result"

# (m6645j) an unrecognized LOOM_SESSION_MODE value is not a licence to allow.
result=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=banana "CLAUDE_PID=2147483647")
assert_block "(m6645j) unrecognized LOOM_SESSION_MODE value -> block (fail closed)" "$result"

# (m6645k) precedence: an explicit interactive override beats the
# LOOM_HEADLESS_SESSION marker AND a print-mode argv (operator escape hatch for
# a misclassified session).
result=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=interactive LOOM_HEADLESS_SESSION=1 "CLAUDE_PID=$FAKE_PRINT_PID")
assert_advisory "(m6645k) LOOM_SESSION_MODE=interactive beats marker + print argv" "$result"

# (m6645l) precedence the safe way round: the marker beats a non-print argv.
result=$(run_hook_detect "$T1" false LOOM_HEADLESS_SESSION=1 "CLAUDE_PID=$FAKE_TTY_PID")
assert_block "(m6645l) LOOM_HEADLESS_SESSION=1 beats a non-print argv -> block" "$result"

# (m6645m) the loop guard is untouched by the new branch: stop_hook_active=true
# allows in BOTH modes, and interactive mode does not even emit the advisory
# (the hook exits at the loop guard, long before any detection runs).
result=$(run_hook_detect "$T1" true LOOM_SESSION_MODE=interactive)
assert_allow "(m6645m) stop_hook_active=true + interactive -> allow, silent" "$result"
result=$(run_hook_detect "$T1" true LOOM_SESSION_MODE=headless)
assert_allow "(m6645m) stop_hook_active=true + headless -> allow, silent" "$result"

# (m6645n) a fully-resolved transcript is silent in interactive mode too -- the
# advisory must fire only when something is actually outstanding, never on
# every stop.
result=$(run_hook_detect "$T2" false LOOM_SESSION_MODE=interactive)
assert_allow "(m6645n) resolved transcript + interactive -> allow, no advisory" "$result"

# (m6645o) the interactive advisory still reports the SAME accounting the block
# would have: it names the outstanding dispatch id and says the stop is allowed.
raw_adv=$(run_hook_detect "$T1" false LOOM_SESSION_MODE=interactive)
adv_msg=$(echo "${raw_adv#*|}" | jq -r '.systemMessage // empty' 2>/dev/null || true)
if [[ "$adv_msg" == *"toolu_01"* && "$adv_msg" == *"INTERACTIVE"* \
      && "$adv_msg" == *"ALLOWED"* && "$adv_msg" == *"#6645"* ]]; then
    pass "(m6645o) interactive advisory names the outstanding id and says ALLOWED"
else
    fail "(m6645o) interactive advisory names the outstanding id and says ALLOWED (got: $adv_msg)"
fi

# (m6645p) the guard toggle still wins over everything: disabled means silent
# in interactive mode too (no advisory leaks out of a disabled guard).
result=$(run_hook_detect "$T1" false LOOM_GUARD_BACKGROUND_SUBAGENTS=0 LOOM_SESSION_MODE=interactive)
assert_allow "(m6645p) guard disabled + interactive -> allow, silent" "$result"

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
