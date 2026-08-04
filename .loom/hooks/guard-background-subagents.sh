#!/usr/bin/env bash
# guard-background-subagents.sh — Stop hook backstop for issue #4257
#
# Mechanical backstop for the hazard documented in
# defaults/.claude/commands/loom/sweep.md under "CRITICAL: Subagent dispatch is
# async-only — you MUST block explicitly (issue #3822)": in headless
# `claude -p` mode, ending the orchestrator's turn terminates the process,
# which kills every still-running background Task subagent. That section is a
# documentation-only guardrail; this hook is the mechanical backstop for when
# an orchestrator forgets it and tries to end its turn anyway.
#
# Contract (Stop hook):
#   Input (JSON on stdin): { "session_id": "...", "transcript_path": "...",
#     "stop_hook_active": true|false, "hook_event_name": "Stop", ... }
#   Output: to block the stop, print `{"decision":"block","reason":"..."}` to
#     stdout and exit 0. To allow the stop, exit 0 with no output.
#
# Detection heuristic: scan the transcript JSONL for two independent
# dispatch-without-observed-completion patterns:
#
#   1. Assistant `tool_use` entries named "Task" OR "Agent" (issue #5086 — the
#      current harness names the async subagent-dispatch tool "Agent", not
#      "Task"; "Task" is matched too for forward/back compat, the same
#      dual-name pattern used below for `Monitor`/`ScheduleWakeup`) whose id
#      has no observed completion anywhere later in the transcript — i.e. a
#      subagent was dispatched and the transcript never observed its
#      completion before the orchestrator tried to end its turn. An `Agent`
#      dispatch gets an IMMEDIATE `tool_result` ack ("Async agent launched
#      successfully... agentId: <ID> ... You will be notified automatically
#      when it completes.") on the SAME tool_use id the real completion later
#      arrives on — that ack is NOT completion, so naively diffing dispatch
#      ids against "any tool_result observed" would (as it did for background
#      Bash in #4389) treat the dispatch as already resolved the instant it
#      fires. Only a LATER, distinct tool_result on that id (the real
#      completion), or an explicit non-error, TERMINAL `TaskOutput` poll of
#      the `agentId` recovered from the launch ack, counts as resolution. A
#      plain `Task`-named dispatch's single ordinary tool_result satisfies the
#      "distinct tool_result" branch directly (it never matches the launch-ack
#      text), so back-compat needs no special casing.
#   2. Assistant `Bash` `tool_use` entries with `input.run_in_background ==
#      true` (issue #4389 — the #4257 recurrence) whose dispatch has no
#      observed completion anywhere later in the transcript. A background Bash
#      dispatch gets an IMMEDIATE `tool_result` ack ("Command running in
#      background with ID: ...") at dispatch time — that ack is NOT
#      completion, so pattern (1)'s tool_result-matching logic would (and
#      did, in the #4347 death) treat it as already resolved. A background
#      Bash task counts as RESOLVED when any of these later events appears for
#      it (issue #5013 broadened this from `<tool-use-id>`-only matching, the
#      analogue of the #4696 Monitor fix):
#        - a `<task-notification>` whose `<tool-use-id>` echoes the dispatch id
#          (the original #4389 signal), OR
#        - a `<task-notification>` whose `<task-id>` is the TASK id from the
#          dispatch ack (`running in background with ID: <ID>`) — some
#          completions carry ONLY `<task-id>`, the Monitor-shaped notification
#          that `<tool-use-id>`-only matching never observed (the constant
#          "1 outstanding" false positive of #5013), OR
#        - a blocking `TaskOutput`/`BashOutput` read of that task (keyed on its
#          task id or dispatch tool-use id) whose result is not an error — in
#          headless mode a blocking read returns only after the task produced
#          its output/completed, and may itself consume the notification, OR
#        - an explicit `TaskStop` of the task id (#4696).
#   3. Assistant `Monitor` / `ScheduleWakeup` `tool_use` entries (issue #4462)
#      that are still armed — i.e. the transcript shows no later event that
#      could have retired the timer. Like a background Bash task, arming a
#      `Monitor` returns an IMMEDIATE `tool_result` ack that is NOT the fire
#      event, so the dispatch-time ack is never treated as resolution. This is
#      the exact #4462 strand: a transport failure (529/Overloaded) handled by
#      arming `Monitor {command: "sleep 90 && …"}` and ending the turn — in
#      headless `-p` mode the timer has no session to wake, the process exits
#      0, and the sweep is orphaned.
#
#      Monitor resolution is keyed on the timer's TASK id, not its tool-use id
#      (issue #4696 — the third transcript-format gap after #4482/#4462).
#      Verified against live transcripts: a Monitor's fired-event
#      `<task-notification>` carries ONLY `<task-id>` — it never emits the
#      `<tool-use-id>` tag a background-Bash completion does. Matching Monitor
#      dispatch ids against `<tool-use-id>` (the #4462 implementation) could
#      therefore NEVER observe a resolution, so every Monitor ever armed
#      re-blocked one stop per stop sequence for the rest of the session. The
#      task id is recovered from the arming ack, whose two real shapes are:
#        `Monitor started (task <ID>, timeout <N>ms). …`
#        `Monitor started (task <ID>, persistent — runs until TaskStop or
#         session end). …`
#      A timer counts as retired when ANY of these appear for its task id:
#        a. `TaskStop` — an assistant `tool_use` named `TaskStop` with
#           `input.task_id == <ID>` (whose result is not a tool_use_error), or
#           any `tool_result` text containing `Successfully stopped task: <ID>`.
#        b. A fired `<task-notification>` whose `<task-id>` is `<ID>` (any
#           status) — the timer was observed doing its job.
#        c. Its own configured timeout elapsing: `timeout <N>ms` from the ack
#           (else `input.timeout_ms` when not persistent) measured from the
#           arming entry's `timestamp`. An expired timer cannot fire again, so
#           it cannot be orphaned. A `persistent` Monitor has no timeout and is
#           deliberately NOT retired this way — only (a) or (b) retire it.
#        d. The arming call itself erroring (`tool_use_error` / `is_error`): no
#           timer exists.
#      All four are durable, append-only transcript facts, so a timer retired
#      once stays retired on every later stop sequence in the same session — no
#      hook-side state is needed and no re-flagging can occur.
#      The legacy `<tool-use-id>` echo is still accepted as resolution for
#      forward compatibility, for the case where no task id can be recovered.
#
#      `ScheduleWakeup` is the same detector but a DIFFERENT set of shapes: its
#      ack is `Next wakeup scheduled for HH:MM:SS (in <N>s). …`, and a fired
#      wakeup re-invokes the session rather than emitting a task-notification —
#      it leaves no task id and no notification at all. It is therefore retired
#      by `(in <N>s)` elapsing since the arming entry's timestamp, by a later
#      `ScheduleWakeup {stop: true}` cancel (ack `Loop stopped — cancelled <N>
#      pending wakeup(s); …`, which arms nothing itself), or by the arming call
#      erroring (`` `prompt` is required when `stop` is not true. ``).
#
# In all three cases, this is a HEURISTIC over the transcript file, not a live
# process check (no such live signal exists here), so it can have false
# positives (e.g. a slow transcript flush) — hence the single-block semantics
# below rather than a hard, repeatable deny.
#
# Loop guard: `stop_hook_active` is true when this hook itself caused an
# earlier block in the current stop sequence. Blocking unconditionally on that
# second pass would wedge the session in an infinite "you must continue" loop
# the orchestrator can never satisfy if the heuristic keeps re-firing (e.g. a
# tool_result that legitimately never lands in this transcript format). So:
# block AT MOST ONCE per stop sequence, then allow.
#
# Toggle: guards.backgroundSubagents (default true) / LOOM_GUARD_BACKGROUND_SUBAGENTS
# env override, same env > config > default precedence as every other guard
# category in this repo (see guard-worktree-paths.sh).
#
# Error handling: this script MUST NEVER exit non-zero, and any unexpected
# error (missing jq, unreadable/unparseable transcript, malformed input)
# fails OPEN (allow the stop) rather than wedging the session.

trap 'exit 0' ERR

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd 2>/dev/null || echo ".")"
MAIN_ROOT="$(cd "$(git -C "$SCRIPT_DIR" rev-parse --git-common-dir 2>/dev/null)/.." 2>/dev/null && pwd)" || \
MAIN_ROOT="$(cd "$SCRIPT_DIR/../.." 2>/dev/null && pwd 2>/dev/null || echo ".")"

# =============================================================================
# Guard toggle — guards.backgroundSubagents / LOOM_GUARD_BACKGROUND_SUBAGENTS
# Default ON. Resolution order (highest precedence first):
#   1. LOOM_GUARD_BACKGROUND_SUBAGENTS env var (0/false/no disables, 1/true/yes forces on)
#   2. .loom/config.json -> guards.backgroundSubagents (default true when absent)
#   3. Default: true (guard on)
# =============================================================================
background_subagent_guard_enabled() {
    local enabled=true
    if [[ -n "$MAIN_ROOT" && -f "$MAIN_ROOT/.loom/config.json" ]] && command -v jq &>/dev/null; then
        enabled=$(jq -r 'if .guards.backgroundSubagents == false then "false" else "true" end' "$MAIN_ROOT/.loom/config.json" 2>/dev/null) || enabled=true
        [[ -n "$enabled" ]] || enabled=true
    fi
    case "${LOOM_GUARD_BACKGROUND_SUBAGENTS:-}" in
        0|false|no)  enabled=false ;;
        1|true|yes)  enabled=true ;;
    esac
    [[ "$enabled" == "true" ]]
}

if ! background_subagent_guard_enabled; then
    exit 0
fi

if ! command -v jq &>/dev/null; then
    exit 0
fi

INPUT=$(cat 2>/dev/null) || INPUT=""
[[ -n "$INPUT" ]] || exit 0

# Loop guard: never block twice in the same stop sequence.
STOP_HOOK_ACTIVE=$(printf '%s' "$INPUT" | jq -r '.stop_hook_active // false' 2>/dev/null) || STOP_HOOK_ACTIVE="false"
if [[ "$STOP_HOOK_ACTIVE" == "true" ]]; then
    exit 0
fi

TRANSCRIPT_PATH=$(printf '%s' "$INPUT" | jq -r '.transcript_path // empty' 2>/dev/null) || TRANSCRIPT_PATH=""
[[ -n "$TRANSCRIPT_PATH" && -r "$TRANSCRIPT_PATH" ]] || exit 0

# Shared jq prelude for the Task/Agent, background-Bash, and Monitor
# detectors below.
#
# `texts` yields every transcript entry's notification-bearing STRING payload.
# A completion `<task-notification>` is NOT a `type=="user"` message (issue
# #4482). Verified against live transcripts, the harness writes it as one (or
# both) of these top-level entry shapes:
#   1. `{"type":"queue-operation", "content":"<task-notification>...</task-notification>", ...}`
#      — the notification text is the top-level `.content` STRING field.
#   2. `{"type":"attachment","attachment":{"commandMode":"task-notification",
#      "prompt":"<task-notification>...</task-notification>"}, ...}`
#      — the notification text is `.attachment.prompt`.
# We scan all three shapes (both real ones above + the legacy `type=="user"`
# string-content path, kept for forward/backward compatibility).
#
# `stopped_task_ids` yields every TASK id the transcript shows as explicitly
# stopped (issue #4696): from a `TaskStop` tool_use's `input.task_id` (unless
# that call itself errored) and from any `tool_result` text containing the
# harness's `Successfully stopped task: <ID>` confirmation. A stopped task
# cannot be orphaned by ending the turn, so this retires both a Monitor timer
# and a background Bash task.
JQ_PRELUDE='
def texts:
  ( select(.type=="queue-operation") | (.content? // empty) | select(type=="string") ),
  ( select(.type=="attachment") | (.attachment.prompt? // empty) | select(type=="string") ),
  ( select(.type=="user")
    | .message.content as $c
    | ( if ($c|type) == "string" then $c
        else ($c[]? | (.content? // empty) | select(type=="string"))
        end ) );
def entry_ts: ((.timestamp? // empty) | select(type=="string")
               | (sub("\\.[0-9]+Z$";"Z") | fromdateiso8601? // empty));
def results:
  [ .[]? | select(.type=="user") | .message.content[]?
    | select(.type=="tool_result")
    | { id: (.tool_use_id // ""),
        text: (.content | if type=="string" then . else tojson end),
        err: (((.is_error? // false) == true)
              or ((.content | tojson) | test("tool_use_error"))) } ];
def notif_texts: [ .[]? | texts | select(test("<task-notification>")) ];
def stopped_task_ids:
  . as $t
  | (results) as $r
  | ( [ $t[]? | select(.type=="assistant") | .message.content[]?
        | select(.type=="tool_use" and .name=="TaskStop")
        | { id: (.id // ""), task: (.input.task_id? // null) }
        | select(.task != null) ] ) as $stops
  | ( [ $stops[]
        | . as $s
        | (($r | map(select(.id == $s.id)) | .[0]) // null) as $res
        | select($res == null or ($res.err != true))
        | $s.task ] )
    + ( [ $r[] | ((.text | capture("Successfully stopped task: (?<v>[A-Za-z0-9_-]+)")?).v) // empty ] );
'

# Diff Task/Agent subagent-dispatch ids against every event that can retire one
# (issue #5086 — the harness's async subagent-dispatch tool is named "Agent",
# not "Task"; see the header for why a naive rename-only fix reintroduces the
# #4389 false-negative hazard on this tool). A dispatch's tool_use id resolves
# when EITHER of these appears later in the transcript for it:
#
#   a. A tool_result on the SAME id whose text is NOT the immediate "Async
#      agent launched successfully..." launch ack — i.e. a second, distinct
#      tool_result landed on that id, which only happens for the real
#      completion (an Agent dispatch's launch ack and its later completion
#      share one tool_use id). A plain `Task`-named dispatch's single ordinary
#      tool_result also satisfies this branch directly (back-compat: it never
#      matches the launch-ack text, so it counts as its own "distinct"
#      completion — existing Task fixtures need no changes).
#   b. An explicit, non-error, TERMINAL `TaskOutput` poll (`<status>completed
#      </status>` or `<status>failed</status>` in the result text — NOT
#      `<status>running</status>`, which is still pending) of the `agentId`
#      recovered from the launch ack text (`agentId: <ID>`).
#   c. Structural short-circuit (issue #5243): a dispatch with
#      `input.run_in_background == false` is SYNCHRONOUS — the harness cannot
#      advance the assistant's turn (let alone reach a Stop event) past a
#      blocking tool_use until its result has actually landed, so that dispatch's
#      FIRST and only `tool_result` is always the real, final result, never a
#      launch ack (no separate async launch ack is structurally possible for a
#      blocking call). For these ids ANY tool_result resolves the dispatch — the
#      launch-ack text exclusion in (a) is skipped entirely, so a sync completion
#      whose text incidentally contains the "Async agent launched successfully"
#      boilerplate (e.g. shared harness ack wording) still resolves. This mirrors
#      the `.name=="Bash" and (.input.run_in_background == true)` structural
#      filter used by the background-Bash detector below. Only an EXPLICIT
#      `== false` triggers this; an absent field stays on the (a)/(b) path (a
#      plain Task with no field is already resolved by its ordinary tool_result
#      via (a), so back-compat is unchanged).
#
# Deliberately does NOT treat an ASYNC dispatch's launch ack as resolution (that
# is the exact #4389 hazard recurring on this tool) — for async ids only (a) or
# (b) counts. A genuinely orphaned async Agent dispatch (no later distinct
# tool_result, no terminal TaskOutput poll) still blocks, the true positive this
# detector exists for.
UNRESOLVED_TASK_IDS=$(jq -s -r "$JQ_PRELUDE"'
  . as $t
  | (results) as $r
  | [ $t[]? | select(.type=="assistant") | .message.content[]?
      | select(.type=="tool_use" and (.name=="Task" or .name=="Agent"))
      | .id ] as $task_ids
  # Synchronous (blocking) Task/Agent dispatch ids (issue #5243): an EXPLICIT
  # run_in_background == false makes the call block, so its first tool_result is
  # always the real completion — never a launch ack. Any tool_result on these
  # ids resolves the dispatch (launch-ack text exclusion skipped for them).
  | [ $t[]? | select(.type=="assistant") | .message.content[]?
      | select(.type=="tool_use" and (.name=="Task" or .name=="Agent"))
      | select(.input.run_in_background == false)
      | .id ] as $sync_task_ids
  # TaskOutput polls: the poll tool_use id, and the agentId/task ref it names.
  | ( [ $t[]? | select(.type=="assistant") | .message.content[]?
        | select(.type=="tool_use" and .name=="TaskOutput")
        | { id: (.id // ""),
            ref: (.input.agentId? // .input.agent_id? // .input.task_id?
                  // .input.id? // null) } ]
    ) as $polls
  # Refs whose poll returned a non-error, TERMINAL (completed/failed) result.
  | ( [ $polls[]
        | . as $p
        | (($r | map(select(.id == $p.id)) | .[0]) // null) as $res
        | select($res != null and ($res.err != true))
        | select($res.text | test("<status>completed</status>|<status>failed</status>"))
        | $p.ref ] | [ .[] | select(. != null) ]
    ) as $polled_ok_refs
  | [ $task_ids[]
      | . as $id
      | ( [ $r[] | select(.id == $id) ] ) as $id_results
      # Any result on this id that is NOT the launch ack text is a real,
      # distinct completion (branch a).
      | ( [ $id_results[]
            | select((.text | test("Async agent launched successfully")) | not) ]
        ) as $real_completions
      # agentId(s) recovered from any launch-ack result on this id, to check
      # against a terminal TaskOutput poll (branch b).
      | ( [ $id_results[]
            | select(.text | test("Async agent launched successfully"))
            | ((.text | capture("agentId: (?<v>[A-Za-z0-9_-]+)")?).v) // empty ]
        ) as $agent_ids
      | if ($id_results | length) == 0 then $id
        elif (($sync_task_ids | index($id)) != null) then empty
        elif ($real_completions | length) > 0 then empty
        elif ( [ $agent_ids[] | select(($polled_ok_refs | index(.)) != null) ]
               | length ) > 0 then empty
        else $id
        end
    ] | .[]
' "$TRANSCRIPT_PATH" 2>/dev/null) || UNRESOLVED_TASK_IDS=""

# Diff background-Bash dispatch ids (issue #4389) against every event that can
# retire one. A background Bash task is resolved when ANY of these appear later
# in the transcript for it (issue #5013 — the fourth transcript-format gap after
# #4482/#4462/#4696, the exact analogue of the Monitor fix):
#
#   a. A `<task-notification>` whose `<tool-use-id>` echoes the DISPATCH id — the
#      original #4389 signal.
#   b. A `<task-notification>` whose `<task-id>` is the TASK id recovered from the
#      dispatch ack (`running in background with ID: <ID>`). Verified against live
#      transcripts, some background-Bash completions carry ONLY `<task-id>` (the
#      same Monitor-shaped notification the #4696 gap was about) — matching purely
#      on `<tool-use-id>` never observed those, so the task re-blocked one stop per
#      stop sequence for the rest of the session (the constant "1 outstanding"
#      false positive of #5013).
#   c. An explicit `TaskStop` of the task id (#4696).
#   d. A blocking `TaskOutput` / `BashOutput` read of the task — keyed on the task
#      id (`bash_id`/`task_id`/`shell_id`) or the dispatch tool-use id — whose
#      result is NOT an error (issue #5013). In headless mode a blocking output
#      read returns only once the task has produced its output/completed, so a
#      non-error read satisfies the guard for that id even when the async
#      `<task-notification>` was consumed by that read and never separately
#      emitted (or arrived in a different shape). This is the criterion the two
#      awaited-via-TaskOutput async agents in the #5013 report hit.
#
# Deliberately does NOT treat the immediate dispatch-time `tool_result` ack as
# resolution (see header) — only one of (a)–(d) counts. A genuinely running
# background task with none of these still blocks the first stop (true positive
# retained).
#
# Async AGENT dispatches (`Task` tool_use — `subagent_type=…`, possibly with
# `run_in_background: true`) are structurally excluded here: only `.name=="Bash"`
# entries enter $bg_ids, so a subagent dispatch is never miscounted as a
# background Bash task. Task subagents are covered by their own detector
# (UNRESOLVED_TASK_IDS) above.
UNRESOLVED_BG_IDS=$(jq -s -r "$JQ_PRELUDE"'
  . as $t
  | (results) as $r
  | (stopped_task_ids) as $stopped
  | (notif_texts) as $n
  | [ $n[] | ((capture("<tool-use-id>(?<v>[^<]+)</tool-use-id>")?).v) // empty
    ] as $notified_tools
  | [ $n[] | ((capture("<task-id>(?<v>[^<]+)</task-id>")?).v) // empty
    ] as $notified_tasks
  # Task/bash ids read via a blocking TaskOutput/BashOutput whose result is not
  # an error. `ref` is whichever id field the read used to name the task; the
  # read may also name the original dispatch tool-use id, so both are matched
  # against the bg dispatch below.
  | ( [ $t[]? | select(.type=="assistant") | .message.content[]?
        | select(.type=="tool_use"
                 and (.name=="TaskOutput" or .name=="BashOutput"
                      or .name=="AsyncTaskOutput"))
        | { id: (.id // ""),
            ref: (.input.task_id? // .input.bash_id? // .input.shell_id?
                  // .input.id? // null) } ] ) as $reads
  | ( [ $reads[]
        | . as $rd
        | (($r | map(select(.id == $rd.id)) | .[0]) // null) as $res
        | select($res == null or ($res.err != true))
        | { ref: $rd.ref, id: $rd.id } ]
      | [ .[].ref | select(. != null) ] + [ .[].id | select(. != "") ]
    ) as $read_refs
  | [ $t[]? | select(.type=="assistant") | .message.content[]?
      | select(.type=="tool_use" and .name=="Bash" and (.input.run_in_background == true))
      | .id ] as $bg_ids
  | [ $bg_ids[]
      | . as $id
      | (($r | map(select(.id == $id)) | .[0]) // null) as $ack
      | (if $ack == null then null
         else ((($ack.text | capture("background with ID: (?<v>[A-Za-z0-9_-]+)")?).v) // null)
         end) as $tid
      | if (($notified_tools | index($id)) != null) then empty
        elif ($tid != null and ($notified_tasks | index($tid)) != null) then empty
        elif ($tid != null and ($stopped | index($tid)) != null) then empty
        elif ($tid != null and ($read_refs | index($tid)) != null) then empty
        elif (($read_refs | index($id)) != null) then empty
        else $id end
    ] | .[]
' "$TRANSCRIPT_PATH" 2>/dev/null) || UNRESOLVED_BG_IDS=""

# Diff armed Monitor / ScheduleWakeup timers (issue #4462) against every event
# that can retire one (issue #4696 — see the header for why `<tool-use-id>`
# matching alone never worked here). The two tools have DIFFERENT arming acks
# and therefore different retirement shapes; all of them, enumerated from live
# transcripts, are handled below:
#
#   Monitor  "Monitor started (task <ID>, timeout <N>ms). …"
#            "Monitor started (task <ID>, persistent — runs until TaskStop or
#             session end). …"
#     retired by: TaskStop of <ID>; a fired `<task-notification>` whose
#     `<task-id>` is <ID>; `timeout <N>ms` elapsing since the arming entry's
#     timestamp (a `persistent` Monitor has NO self-timeout and is retired only
#     by TaskStop or a fired event); or the arming call erroring.
#
#   ScheduleWakeup  "Next wakeup scheduled for HH:MM:SS (in <N>s). Nothing more
#                    to do this turn — the harness re-invokes you when the
#                    wakeup fires or a task-notification arrives."
#                   "Loop stopped — cancelled <N> pending wakeup(s); …"  (the
#                    `{stop: true}` cancel call — arms nothing itself)
#     retired by: `(in <N>s)` elapsing since the arming entry's timestamp (a
#     fired wakeup re-invokes the session; it does not leave a task id or a
#     notification), a LATER `{stop: true}` cancel (which cancels every pending
#     wakeup), or the arming call erroring (e.g. "`prompt` is required when
#     `stop` is not true.").
#
# An armed-but-unretired timer left as the only pending work is the #4462
# transport-failure strand: in headless `-p` mode the turn end kills the process
# before the timer fires, exit 0 orphans the claim.
UNRESOLVED_MONITOR_IDS=$(jq -s -r "$JQ_PRELUDE"'
  . as $t
  | (results) as $r
  | (notif_texts) as $n
  | [ $n[] | ((capture("<task-id>(?<v>[^<]+)</task-id>")?).v) // empty ] as $notified_tasks
  | [ $n[] | ((capture("<tool-use-id>(?<v>[^<]+)</tool-use-id>")?).v) // empty ] as $notified_tools
  | (stopped_task_ids) as $stopped
  # Entry indices of every ScheduleWakeup {stop:true} cancel; a cancel retires
  # every wakeup armed BEFORE it.
  | [ ($t | to_entries)[] | . as $e | select(.value.type=="assistant")
      | .value.message.content[]?
      | select(.type=="tool_use" and .name=="ScheduleWakeup"
               and ((.input.stop? // false) == true))
      | $e.key ] as $wake_cancels
  | [ ($t | to_entries)[] | . as $e | select(.value.type=="assistant")
      | .value.message.content[]?
      | select(.type=="tool_use" and (.name=="Monitor" or .name=="ScheduleWakeup"))
      | select((.input.stop? // false) != true)     # a {stop:true} call arms nothing
      | { id: (.id // ""),
          name: .name,
          idx: $e.key,
          at: (($e.value | entry_ts) // null),
          cfg_timeout: (.input.timeout_ms? // null),
          cfg_delay: (.input.delaySeconds? // null),
          cfg_persistent: ((.input.persistent? // false) == true) } ] as $armed
  | [ $armed[]
      | . as $m
      | (($r | map(select(.id == $m.id)) | .[0]) // null) as $ack
      | (if $ack == null then "" else $ack.text end) as $txt
      | if ($ack != null and $ack.err) then empty          # arm failed: no timer exists
        elif ($txt | test("Loop stopped")) then empty      # cancel ack: nothing armed
        else
          ((($txt | capture("Monitor started \\(task (?<v>[A-Za-z0-9_-]+)")?).v) // null) as $tid
        # Seconds until this timer retires itself, or null when it never does.
        | (if ($txt | test("Monitor started \\(task [A-Za-z0-9_-]+, persistent"))
             then null                                     # persistent: no self-timeout
           elif ((($txt | capture("timeout (?<v>[0-9]+)ms")?).v) // null) != null
             then (($txt | capture("timeout (?<v>[0-9]+)ms").v | tonumber) / 1000)
           elif ((($txt | capture("\\(in (?<v>[0-9]+)s\\)")?).v) // null) != null
             then ($txt | capture("\\(in (?<v>[0-9]+)s\\)").v | tonumber)
           elif ($m.cfg_persistent | not) and $m.cfg_timeout != null
             then ($m.cfg_timeout / 1000)
           elif $m.cfg_delay != null then $m.cfg_delay
           else null end) as $tmo
        | if ($tid != null and ($stopped | index($tid)) != null) then empty
          elif ($tid != null and ($notified_tasks | index($tid)) != null) then empty
          elif (($notified_tools | index($m.id)) != null) then empty
          elif ($tmo != null and $m.at != null and (now - $m.at) >= $tmo) then empty
          elif ($m.name == "ScheduleWakeup"
                and (($wake_cancels | map(select(. > $m.idx)) | length) > 0)) then empty
          else $m.id end
        end
    ] | .[]
' "$TRANSCRIPT_PATH" 2>/dev/null) || UNRESOLVED_MONITOR_IDS=""

[[ -n "$UNRESOLVED_TASK_IDS" || -n "$UNRESOLVED_BG_IDS" || -n "$UNRESOLVED_MONITOR_IDS" ]] || exit 0

TASK_COUNT=0
[[ -z "$UNRESOLVED_TASK_IDS" ]] || TASK_COUNT=$(printf '%s\n' "$UNRESOLVED_TASK_IDS" | grep -c . || true)
BG_COUNT=0
[[ -z "$UNRESOLVED_BG_IDS" ]] || BG_COUNT=$(printf '%s\n' "$UNRESOLVED_BG_IDS" | grep -c . || true)
MONITOR_COUNT=0
[[ -z "$UNRESOLVED_MONITOR_IDS" ]] || MONITOR_COUNT=$(printf '%s\n' "$UNRESOLVED_MONITOR_IDS" | grep -c . || true)

REASON="STOP BLOCKED (guard-background-subagents.sh, issues #4257/#4389/#4462/#4696/#5013/#5086):"
if [[ "$TASK_COUNT" -gt 0 ]]; then
    REASON="${REASON} ${TASK_COUNT} dispatched Task/Agent subagent(s) have no observed completion in this transcript yet."
fi
if [[ "$BG_COUNT" -gt 0 ]]; then
    REASON="${REASON} ${BG_COUNT} background Bash command(s) (run_in_background) have no completion notification in this transcript yet."
fi
if [[ "$MONITOR_COUNT" -gt 0 ]]; then
    REASON="${REASON} ${MONITOR_COUNT} armed Monitor/ScheduleWakeup timer(s) are still live in this transcript -- no TaskStop, no fired task-notification, and no elapsed timeout for them (issues #4462/#4696) -- a transport-failure backoff (529/Overloaded) MUST be retried inline in the same turn, or the orchestrator must exit NONZERO, never parked on an end-of-turn timer. TaskStop each timer you no longer need."
fi
REASON="${REASON} In headless \`claude -p\` mode, ending this turn TERMINATES THE PROCESS and kills every still-running background child -- there is no 'it finishes after I stop talking'. Before writing a final message, you MUST explicitly await each dispatched subagent's completion (blocking TaskOutput / completion notification), each background Bash task's completion notification, and each armed Monitor/ScheduleWakeup timer's fire event -- see defaults/.claude/commands/loom/sweep.md, 'CRITICAL: Subagent dispatch is async-only' (#3822). If you are certain every subagent/background task/timer has actually finished (e.g. this is a false positive from a slow transcript flush), it is safe to stop again -- this guard blocks at most once per stop sequence."

jq -n --arg reason "$REASON" '{decision: "block", reason: $reason}' 2>/dev/null && exit 0

# jq construction failed for some reason -- fall back to a hand-built JSON
# literal so the block decision still lands even if jq -n misbehaves.
ESCAPED=$(printf '%s' "$REASON" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{"decision":"block","reason":"%s"}\n' "$ESCAPED"
exit 0
