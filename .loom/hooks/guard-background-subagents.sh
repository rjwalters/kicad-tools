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
#      fires. A LATER, distinct tool_result on that id (the real completion),
#      an explicit non-error, TERMINAL `TaskOutput` poll of the `agentId`
#      recovered from the launch ack, or a `<task-notification>` (issue #5713
#      — a correctly-awaited async Agent dispatch's completion arrives ONLY as
#      a `<task-notification>`, never a second `tool_result`, so pattern 1 was
#      structurally unable to ever resolve one; the count only grew across a
#      session) counts as resolution. The notification resolves the dispatch
#      when EITHER of these appears, mirroring pattern 2's already-correct
#      background-Bash matching below:
#        - a `<task-notification>` whose `<tool-use-id>` echoes the dispatch
#          tool_use id, OR
#        - a `<task-notification>` whose `<task-id>` equals the `agentId`
#          recovered from the launch ack, for the case where only the task id
#          is echoed.
#      A plain `Task`-named dispatch's single ordinary tool_result satisfies
#      the "distinct tool_result" branch directly (it never matches the
#      launch-ack text), so back-compat needs no special casing.
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
#        - an explicit `TaskStop` of the task id (#4696), OR
#        - the DISPATCH ITSELF erroring (issue #5976): a PreToolUse guard denial
#          or a harness input-validation rejection means the command never ran,
#          so no background task exists to orphan — no task id was ever minted
#          and no notification/read/TaskStop can ever arrive for it. Before
#          #5976 such a dispatch was counted as outstanding on EVERY stop for
#          the rest of the session (the reported "1 background Bash command(s)
#          have no completion notification" false positive in an interactive
#          session, with `pgrep` confirming no live process). Pattern 3 has
#          always retired a failed Monitor arming call for exactly this reason;
#          pattern 1 gets it implicitly, since an error result is by definition
#          not the launch-ack text and so counts as a distinct completion.
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
#      Loop-continuation exemption (issue #6175). A `ScheduleWakeup` whose
#      `input.prompt` re-arms an interactive `/loop`-style continuation —
#      recognized as a prompt that starts with `/loop` (optionally followed by
#      arguments) or contains the `<<autonomous-loop-dynamic>>` sentinel — is
#      NOT counted as outstanding even while still armed. Re-arming the next
#      wakeup on every iteration is how such a loop stays alive across turn
#      boundaries in an interactive session; ending the turn does not kill it
#      (unlike the headless `-p` orphaning hazard this guard exists to catch),
#      so treating the armed timer as an un-awaited child and blocking on it is
#      a false positive by construction, repeating once per loop iteration.
#      This exemption does NOT weaken detection of a genuinely orphaned timer:
#      only `ScheduleWakeup` dispatches (never `Monitor`) with a recognized
#      loop-re-entry prompt are exempted, everything else is unaffected, and
#      the block reason below still names any recognized loop-continuation
#      timer separately from a truly orphaned one so the transcript stays
#      legible.
#
# In all three cases, this is a HEURISTIC over the transcript file, not a live
# process check (no such live signal exists here), so it can have false
# positives (e.g. a slow transcript flush) — hence the single-block semantics
# below rather than a hard, repeatable deny.
#
# Why NOT corroborate with a liveness check (considered and rejected, #5976).
# Issue #5976 asked whether the transcript accounting above should be confirmed
# against a live signal (a task registry, or `pgrep` for the spawned process)
# before blocking. It should not, because no sound signal exists at this seam:
#   - There is no task registry. The harness owns the background shells and
#     exports no id→pid mapping; the only handle the transcript carries is an
#     opaque `<task-id>` / tool-use id, which no OS-level query can resolve.
#   - `pgrep` cannot be made specific. Without an id→pid mapping the only
#     available predicate is a pattern match on the dispatched command text,
#     which is wrong in both directions: it matches a *different* session's
#     identical command (false "still live", so a real orphan is missed once
#     the fleet runs two sweeps of the same shape), and it misses a task whose
#     shell has forked/exec'd past the matched text (false "finished", which is
#     the #4257 death this guard exists to prevent).
#   - A liveness check answers a different question anyway. A completion
#     notification means "the background *task* exited", not "the work that
#     task was watching finished" — #5976 also reported a `gh run watch` that
#     died on a transient TLS error and notified normally while its CI run was
#     still in flight. Neither transcript accounting nor `pgrep` can close that
#     gap; only the agent re-checking the watched resource can, which is a
#     prompt-level discipline, not a Stop-hook one.
# So the accounting stays transcript-only, and correctness work goes into
# retiring dispatches that provably cannot be outstanding — the #5976 fix
# retires a dispatch whose own ack was an ERROR (pattern 2 below, branch (e)):
# the command never ran, so no task exists to orphan, and the tool-use ids that
# ARE counted are now named in the block reason so a false positive is one grep
# to confirm instead of a manual elimination round.
#
# Context-safe await recipe (issue #6168). Earlier revisions of the block
# message below told the orchestrator to await a Task/Agent subagent via a
# flat "blocking TaskOutput / completion notification" — but a blocking
# `TaskOutput` on a still-running `local_agent` task is the wrong tool by the
# harness's own documentation: on timeout it can return the raw `.output`
# file, the full subagent conversation transcript (JSONL), which is exactly
# the context-window overflow that same documentation warns against (observed
# live: a `TaskOutput(block=true, timeout=600000)` call returned a
# multi-kilobyte raw JSONL dump). The block message now names two different
# recipes depending on session mode instead of one blocking call:
#   - Interactive session: background agents keep running across turns, so
#     just end the turn and let the completion notification arrive on a
#     later turn — no blocking TaskOutput call needed.
#   - Headless `-p` mode: no later turn exists (ending the turn kills the
#     process, see below), so await in-turn with a bounded, NON-BLOCKING
#     `TaskOutput` poll loop (`block: false` or a short `timeout`, sleeping
#     between checks, reading only the result's `<status>` tag) instead of
#     one large blocking call with a long timeout.
#
# Session-mode detection (issue #6645). Earlier revisions could not tell the
# two modes apart, so they named BOTH recipes in one block message and blocked
# the stop unconditionally — which meant that in an INTERACTIVE session the
# guard blocked the very action its own message prescribed ("just end the turn
# and let the notification arrive on a later turn"). The only way through was
# to emit a no-op message and stop a second time, so every wait cost a forced
# double-stop plus a user-visible "STOP BLOCKED" banner, repeating once per
# stop sequence for the rest of the session. This hook now classifies the
# session and blocks ONLY in headless mode; in interactive mode it allows the
# stop and emits at most a one-line `systemMessage` advisory.
#
# `[ -t 0 ]` does NOT work here: a Stop hook's stdin is the pipe carrying its
# JSON payload, so a TTY probe reads non-interactive in BOTH modes. The
# classification therefore uses `loom_session_mode()` below, whose resolution
# order is (highest precedence first):
#
#   1. `LOOM_SESSION_MODE` = `headless`|`print` / `interactive`|`tty` — an
#      explicit operator override (and the test seam this suite pins).
#   2. `LOOM_HEADLESS_SESSION` truthy — the Loom-owned marker BOTH sanctioned
#      headless dispatch paths export for a print-mode spawn (and only for
#      print mode): `defaults/scripts/spawn-claude.sh` and
#      `defaults/scripts/claude-wrapper.sh`. Both matter: `loom-daemon` spawns
#      claude-wrapper.sh DIRECTLY, not via spawn-claude.sh (verified live —
#      the chain is loom-daemon -> claude-wrapper.sh -p ... -> claude -p ...),
#      so the spawn-claude.sh export alone would miss every daemon sweep.
#      Defense in depth for (4): if the harness ever stops carrying `-p` in
#      its argv, Loom-dispatched sweeps are still classified headless.
#   3. `CLAUDE_CODE_ENTRYPOINT` matching `sdk*` — an SDK-driven session is
#      programmatic, never a human at a terminal.
#   4. The owning `claude` process's own argv: a `-p` / `--print` token means
#      print mode, i.e. headless, BY DEFINITION rather than by proxy. The
#      process is located via `CLAUDE_PID` (harness-exported and authoritative
#      when present — set-but-unresolvable means our view of the process tree
#      disagrees with the harness's, which resolves to headless) and, when
#      CLAUDE_PID is absent, by walking this hook's parent chain for a process
#      whose argv looks like the Claude Code CLI.
#   5. Otherwise: HEADLESS. This is the deliberate fail-CLOSED default and the
#      safety floor of this whole change — every path that cannot positively
#      establish "a human is driving this session" keeps the pre-#6645
#      blocking behaviour. Weakening the headless block silently reintroduces
#      the #4257 orphaned-subagent kill hazard, whereas an interactive session
#      misclassified as headless merely costs the old friction.
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

# A daemon roll is pausing this session (#10830, design §2 step 5): its
# background children are about to be stopped with the whole tree, so blocking
# the stop would only hold the session past its safe point. roll-pause.sh asks
# `loom-daemon roll-pause active`; inert without LOOM_DAEMON_ITEM_ID.
if [[ -n "${LOOM_DAEMON_ITEM_ID:-}" ]] && bash "$SCRIPT_DIR/roll-pause.sh" active </dev/null >/dev/null 2>&1; then
    exit 0
fi

# =============================================================================
# Session-mode detection (issue #6645) — see the header for the full rationale.
#
# Prints exactly one of `headless` / `interactive`. EVERY inconclusive path
# resolves to `headless`, which is the pre-#6645 behaviour: this function may
# only ever RELAX the guard on positive evidence that a human is driving the
# session, never on the absence of evidence.
#
# All helpers below are invoked from `if`/`&&`/`||` conditions only, so their
# deliberate non-zero returns never trip this script's `trap 'exit 0' ERR`.
# =============================================================================

# Print a process's argv, one token per line. Linux reads the authoritative
# NUL-separated /proc/<pid>/cmdline; elsewhere it falls back to `ps -o args=`
# (space-joined, so a token containing spaces splits — harmless here, since a
# spurious split can only ever manufacture a `-p` token, i.e. fail closed).
_bgs_proc_argv() {
    local pid="$1"
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if [[ -r "/proc/$pid/cmdline" ]]; then
        tr '\0' '\n' < "/proc/$pid/cmdline" 2>/dev/null || return 1
        return 0
    fi
    command -v ps >/dev/null 2>&1 || return 1
    ps -o args= -p "$pid" 2>/dev/null | tr ' ' '\n' || return 1
}

_bgs_parent_pid() {
    local pid="$1" ppid=""
    [[ "$pid" =~ ^[0-9]+$ ]] || return 1
    if [[ -r "/proc/$pid/status" ]]; then
        ppid=$(awk '/^PPid:/ { print $2; exit }' "/proc/$pid/status" 2>/dev/null) || ppid=""
    fi
    if [[ ! "$ppid" =~ ^[0-9]+$ ]] && command -v ps >/dev/null 2>&1; then
        ppid=$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]') || ppid=""
    fi
    [[ "$ppid" =~ ^[0-9]+$ ]] || return 1
    printf '%s' "$ppid"
}

# True when <pid>'s argv looks like the Claude Code CLI itself. Only the first
# two tokens are considered, so `bash .../claude-wrapper.sh -p …` (argv[1]
# basename `claude-wrapper.sh`) is deliberately NOT matched — the walk
# continues past it to the real `claude` process.
_bgs_is_claude_proc() {
    local argv tok base i=0
    argv=$(_bgs_proc_argv "$1") || return 1
    [[ -n "$argv" ]] || return 1
    while IFS= read -r tok; do
        i=$((i + 1))
        [[ "$i" -le 2 ]] || break
        [[ -n "$tok" ]] || continue
        base="${tok##*/}"
        case "$base" in
            claude | claude.exe | claude.js) return 0 ;;
            cli.js) [[ "$tok" == *claude* ]] && return 0 ;;
        esac
    done <<< "$argv"
    return 1
}

# True when <pid>'s argv carries a print-mode flag — the definition of a
# headless `claude -p` session, not a proxy for it.
_bgs_argv_has_print_flag() {
    local argv tok
    argv=$(_bgs_proc_argv "$1") || return 1
    [[ -n "$argv" ]] || return 1
    while IFS= read -r tok; do
        case "$tok" in
            -p | --print | --print=*) return 0 ;;
        esac
    done <<< "$argv"
    return 1
}

# Locate the `claude` process that owns this hook.
#
# When the harness exports CLAUDE_PID it is AUTHORITATIVE: if it resolves to a
# `claude` process, that is the answer; if it is set but does NOT resolve, our
# view of the process tree disagrees with the harness's, so we report failure
# (-> fail closed -> headless) rather than walking the tree and trusting
# whatever we happen to find. The ancestor walk is the fallback for the case
# where CLAUDE_PID is absent entirely.
_bgs_find_claude_pid() {
    local pid
    if [[ -n "${CLAUDE_PID:-}" ]]; then
        if _bgs_is_claude_proc "${CLAUDE_PID}"; then
            printf '%s' "${CLAUDE_PID}"
            return 0
        fi
        return 1
    fi
    pid="$$"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        pid=$(_bgs_parent_pid "$pid") || return 1
        [[ "$pid" != "0" && "$pid" != "1" ]] || return 1
        if _bgs_is_claude_proc "$pid"; then
            printf '%s' "$pid"
            return 0
        fi
    done
    return 1
}

loom_session_mode() {
    local mode="" cpid=""
    case "${LOOM_SESSION_MODE:-}" in
        headless | print) mode=headless ;;
        interactive | tty) mode=interactive ;;
    esac
    if [[ -z "$mode" ]]; then
        case "${LOOM_HEADLESS_SESSION:-}" in
            1 | true | yes | on) mode=headless ;;
        esac
    fi
    if [[ -z "$mode" ]]; then
        case "${CLAUDE_CODE_ENTRYPOINT:-}" in
            sdk*) mode=headless ;;
        esac
    fi
    if [[ -z "$mode" ]]; then
        if cpid=$(_bgs_find_claude_pid) && [[ -n "$cpid" ]]; then
            if _bgs_argv_has_print_flag "$cpid"; then
                mode=headless
            else
                mode=interactive
            fi
        else
            # No owning `claude` process could be identified — fail CLOSED.
            mode=headless
        fi
    fi
    printf '%s' "$mode"
}

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
#   d. A `<task-notification>` (issue #5713): the only completion signal a
#      correctly-awaited async Agent dispatch actually produces in this
#      harness is a `<task-notification>` carrying BOTH a `<task-id>` and a
#      `<tool-use-id>` matching the original dispatch — never a second,
#      distinct `tool_result` on the dispatch id. Pattern 2 (background Bash,
#      below) already accepts this evidence; pattern 1 did not, so a
#      correctly-awaited agent could never resolve and the unresolved count
#      only grew across a session. Resolves when EITHER of these appears:
#        - a `<task-notification>` whose `<tool-use-id>` echoes the dispatch
#          tool_use id, OR
#        - a `<task-notification>` whose `<task-id>` equals an `agentId`
#          recovered from a launch-ack result on this id (branch b's ref),
#          for the case where only the task id is echoed.
#
# Deliberately does NOT treat an ASYNC dispatch's launch ack as resolution (that
# is the exact #4389 hazard recurring on this tool) — for async ids only (a),
# (b), or (d) counts. A genuinely orphaned async Agent dispatch (no later
# distinct tool_result, no terminal TaskOutput poll, no completion
# notification) still blocks, the true positive this detector exists for.
UNRESOLVED_TASK_IDS=$(jq -s -r "$JQ_PRELUDE"'
  . as $t
  | (results) as $r
  | (notif_texts) as $n
  | [ $n[] | ((capture("<tool-use-id>(?<v>[^<]+)</tool-use-id>")?).v) // empty
    ] as $notified_tools
  | [ $n[] | ((capture("<task-id>(?<v>[^<]+)</task-id>")?).v) // empty
    ] as $notified_tasks
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
      | if (($notified_tools | index($id)) != null) then empty
        elif ( [ $agent_ids[] | . as $aid
                 | select(($notified_tasks | index($aid)) != null) ]
               | length ) > 0 then empty
        elif ($id_results | length) == 0 then $id
        elif (($sync_task_ids | index($id)) != null) then empty
        elif ($real_completions | length) > 0 then empty
        # NOTE: must bind the loop item to a variable before the lookup — a bare
        # `index` call fed the bare `.` filter would rebind `.` to
        # $polled_ok_refs itself (via the preceding `|`), evaluating
        # `index($polled_ok_refs)`, which returns 0 (a match) whenever
        # $polled_ok_refs is merely non-empty, regardless of the actual agent
        # id (#5721).
        elif ( [ $agent_ids[] | . as $aid
                 | select(($polled_ok_refs | index($aid)) != null) ]
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
      # (e) the dispatch itself ERRORED (issue #5976): a PreToolUse guard denial
      # or a harness input-validation rejection means the command never ran, so
      # no background task was ever created — no task id was minted, and no
      # notification, blocking read or TaskStop can ever exist for it. Without
      # this branch such a dispatch is counted as outstanding on EVERY stop for
      # the rest of the session (the reported false positive). This mirrors the
      # long-standing "arming call errored: no timer exists" branch in the
      # Monitor detector, which the background-Bash detector never had.
      # NOTE: no apostrophes in this block -- the whole jq program is a
      # single-quoted bash string.
      | if ($ack != null and $ack.err) then empty
        elif (($notified_tools | index($id)) != null) then empty
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
#
# Loop-continuation exemption (issue #6175): a still-armed `ScheduleWakeup`
# whose `input.prompt` recognizably re-arms an interactive `/loop`-style
# continuation is tagged `LOOP:<id>` instead of being treated as outstanding —
# re-arming the wakeup every iteration IS how such a loop stays alive, so a
# turn ending without retiring it is expected, not orphaned. Everything else
# (a genuinely un-awaited Monitor or ScheduleWakeup, including a ScheduleWakeup
# whose prompt does not match the loop pattern) is tagged `ORPHAN:<id>` and
# still blocks — the header's "why NOT corroborate with a liveness check"
# rationale (#5976) applies here too: this is a text heuristic over the
# prompt, not a semantic guarantee the prompt actually drives a loop, so it is
# scoped as narrowly as the acceptance criteria allow (ScheduleWakeup only,
# never Monitor; a recognized prefix/sentinel, not "any prompt is present").
MONITOR_TAGGED=$(jq -s -r "$JQ_PRELUDE"'
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
          cfg_persistent: ((.input.persistent? // false) == true),
          # /loop re-entry recognition (#6175): a ScheduleWakeup prompt that
          # starts with "/loop" (optionally followed by arguments) or carries
          # the "<<autonomous-loop-dynamic>>" sentinel is a recognized loop
          # continuation. Monitor never matches (it has no prompt field).
          is_loop: (.name == "ScheduleWakeup"
                    and (((.input.prompt? // "") | test("^\\s*/loop(\\s|$)"))
                         or ((.input.prompt? // "") | test("<<autonomous-loop-dynamic>>")))) } ] as $armed
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
          elif $m.is_loop then ("LOOP:" + $m.id)
          else ("ORPHAN:" + $m.id) end
        end
    ] | .[]
' "$TRANSCRIPT_PATH" 2>/dev/null) || MONITOR_TAGGED=""

UNRESOLVED_MONITOR_IDS=$(printf '%s\n' "$MONITOR_TAGGED" | grep '^ORPHAN:' | sed 's/^ORPHAN://') || UNRESOLVED_MONITOR_IDS=""
UNRESOLVED_LOOP_IDS=$(printf '%s\n' "$MONITOR_TAGGED" | grep '^LOOP:' | sed 's/^LOOP://') || UNRESOLVED_LOOP_IDS=""

[[ -n "$UNRESOLVED_TASK_IDS" || -n "$UNRESOLVED_BG_IDS" || -n "$UNRESOLVED_MONITOR_IDS" ]] || exit 0

TASK_COUNT=0
[[ -z "$UNRESOLVED_TASK_IDS" ]] || TASK_COUNT=$(printf '%s\n' "$UNRESOLVED_TASK_IDS" | grep -c . || true)
BG_COUNT=0
[[ -z "$UNRESOLVED_BG_IDS" ]] || BG_COUNT=$(printf '%s\n' "$UNRESOLVED_BG_IDS" | grep -c . || true)
MONITOR_COUNT=0
[[ -z "$UNRESOLVED_MONITOR_IDS" ]] || MONITOR_COUNT=$(printf '%s\n' "$UNRESOLVED_MONITOR_IDS" | grep -c . || true)
LOOP_COUNT=0
[[ -z "$UNRESOLVED_LOOP_IDS" ]] || LOOP_COUNT=$(printf '%s\n' "$UNRESOLVED_LOOP_IDS" | grep -c . || true)

# Name the ids each detector believes are outstanding (issue #5976). Without
# them, confirming a false positive means eliminating every dispatch in the
# session by hand — the ids turn that into one grep of the transcript. Bounded
# to MAX_LISTED_IDS so a session with many outstanding dispatches cannot emit
# an unbounded reason string.
MAX_LISTED_IDS=8
format_id_list() {
    local ids="$1" total listed out="" id
    total=$(printf '%s\n' "$ids" | grep -c . || true)
    [[ "$total" -gt 0 ]] || return 0
    while IFS= read -r id; do
        [[ -n "$id" ]] || continue
        if [[ -z "$out" ]]; then out="$id"; else out="${out}, ${id}"; fi
    done < <(printf '%s\n' "$ids" | grep . | head -"$MAX_LISTED_IDS")
    listed=$((total > MAX_LISTED_IDS ? MAX_LISTED_IDS : total))
    if [[ "$total" -gt "$listed" ]]; then
        out="${out}, +$((total - listed)) more"
    fi
    printf ' [%s]' "$out"
}

# The factual inventory of what is still outstanding. Shared verbatim by the
# headless block reason and the interactive advisory below, so both modes
# report the SAME accounting and only the prescription differs.
SUMMARY=""
if [[ "$TASK_COUNT" -gt 0 ]]; then
    SUMMARY="${SUMMARY} ${TASK_COUNT} dispatched Task/Agent subagent(s)$(format_id_list "$UNRESOLVED_TASK_IDS") have no observed completion in this transcript yet."
fi
if [[ "$BG_COUNT" -gt 0 ]]; then
    SUMMARY="${SUMMARY} ${BG_COUNT} background Bash command(s) (run_in_background)$(format_id_list "$UNRESOLVED_BG_IDS") have no completion notification in this transcript yet."
fi
if [[ "$MONITOR_COUNT" -gt 0 ]]; then
    SUMMARY="${SUMMARY} ${MONITOR_COUNT} armed Monitor/ScheduleWakeup timer(s)$(format_id_list "$UNRESOLVED_MONITOR_IDS") are still live in this transcript -- no TaskStop, no fired task-notification, and no elapsed timeout for them (issues #4462/#4696) -- these are ORPHANED timers (not a recognized /loop continuation). TaskStop each timer you no longer need."
fi
if [[ "$LOOP_COUNT" -gt 0 ]]; then
    SUMMARY="${SUMMARY} (Informational, NOT counted above and NOT blocking: ${LOOP_COUNT} ScheduleWakeup loop-continuation timer(s)$(format_id_list "$UNRESOLVED_LOOP_IDS") were recognized as an intentional /loop re-entry -- allowed. Re-arming a wakeup every iteration is how that loop stays alive across turn boundaries; do not TaskStop it to satisfy this guard.)"
fi

SESSION_MODE=$(loom_session_mode) || SESSION_MODE=headless
[[ "$SESSION_MODE" == "interactive" || "$SESSION_MODE" == "headless" ]] || SESSION_MODE=headless

if [[ "$SESSION_MODE" == "interactive" ]]; then
    # Interactive session (issue #6645): background children SURVIVE the turn
    # boundary here and their completion notifications arrive on a later turn,
    # so ending the turn is the correct recipe -- exactly the action earlier
    # revisions of this guard prescribed and then blocked. Allow the stop and
    # say so once, as a `systemMessage` advisory; never a "STOP BLOCKED".
    ADVISORY="Note (guard-background-subagents.sh, #6645): this turn ended with work still outstanding.${SUMMARY} This session was detected as INTERACTIVE, where ending the turn does NOT kill background children -- their completion notifications arrive on a later turn -- so the stop is ALLOWED and no action is required. (If this session is really headless and you want the hard block back, set LOOM_SESSION_MODE=headless.)"
    jq -n --arg m "$ADVISORY" '{continue: true, systemMessage: $m}' 2>/dev/null
    exit 0
fi

# Headless (or undetermined -> fail-closed) session: ending the turn kills
# every still-running background child, so this is the #4257 hazard and the
# stop is blocked, exactly as it was before #6645.
REASON="STOP BLOCKED (guard-background-subagents.sh, issues #4257/#4389/#4462/#4696/#5013/#5086/#5976/#6175):${SUMMARY}"
if [[ "$MONITOR_COUNT" -gt 0 ]]; then
    REASON="${REASON} An orphaned timer BLOCKS this stop: a transport-failure backoff (529/Overloaded) MUST be retried inline in the same turn, or the orchestrator must exit NONZERO, never parked on an end-of-turn timer."
fi
REASON="${REASON} This session was classified as HEADLESS \`claude -p\` mode (see #6645 -- LOOM_SESSION_MODE / LOOM_HEADLESS_SESSION / the owning claude process's argv; an undetermined session is treated as headless on purpose). In HEADLESS mode, ending this turn TERMINATES THE PROCESS and kills every still-running background child -- there is no 'it finishes after I stop talking'. Before writing a final message, you MUST explicitly await each one, with a CONTEXT-SAFE recipe (issue #6168 -- a blocking \`TaskOutput\` on a still-running local_agent Task/Agent subagent can time out and return the raw JSONL transcript dump instead of just status, overflowing your context): poll each dispatched Task/Agent subagent in-turn with a bounded, NON-BLOCKING \`TaskOutput\` loop (\`block: false\` or a short timeout, sleeping between checks, reading only the result's <status> tag) instead of one big blocking call. Also await each background Bash task's completion notification (or a bounded BashOutput poll), and each armed Monitor/ScheduleWakeup timer's fire event -- see defaults/.claude/commands/loom/sweep.md, 'CRITICAL: Subagent dispatch is async-only' (#3822). If you are certain every subagent/background task/timer has actually finished (e.g. this is a false positive from a slow transcript flush), it is safe to stop again -- this guard blocks at most once per stop sequence."

jq -n --arg reason "$REASON" '{decision: "block", reason: $reason}' 2>/dev/null && exit 0

# jq construction failed for some reason -- fall back to a hand-built JSON
# literal so the block decision still lands even if jq -n misbehaves.
ESCAPED=$(printf '%s' "$REASON" | sed 's/\\/\\\\/g; s/"/\\"/g')
printf '{"decision":"block","reason":"%s"}\n' "$ESCAPED"
exit 0
