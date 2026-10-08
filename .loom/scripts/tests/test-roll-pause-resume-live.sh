#!/usr/bin/env bash
# test-roll-pause-resume-live.sh — LIVE pause-and-resume test for a daemon roll
# (issue #10830, the acceptance gate of design
# docs/design/daemon-roll-pause-resume.md §11 PR 1).
#
# OPT-IN. Uses real credentials and real model calls, so it never runs in CI
# (ci-excluded.txt) and skips unless LOOM_LIVE_ROLL_PAUSE_TEST=1. Run it on the
# gate host and attach its output to the PR.
#
# For each runtime (Claude Code on the host, Codex inside a session container)
# it starts a headless session THROUGH THE SPAWN SCRIPTS on a scripted
# multi-tool task that also starts a `setsid`'d dummy dev server, then:
#   (a) raises a pause request mid-task and checks the session parks at a safe
#       point: the previous step finished and the parked step has not run;
#   (b) stops the whole process tree and checks the dev server is gone;
#   (c) resumes the session by id in the same cwd (Codex: the same account's
#       CODEX_HOME, inside the still-running container);
#   (d) checks the resumed session recalls a nonce planted before the pause,
#       runs the parked step exactly once, and completes the task.
# It also RECORDS what a parked call does when the park window runs out (the
# deny; gated only on the call never running), how each runtime's transcript shows the
# dangling tool call after resume, Claude's behaviour when a hook outlives its
# own timeout, and the restart cost of a `Task` subagent interrupted mid-task.
#
# Environment:
#   LOOM_LIVE_ROLL_PAUSE_TEST=1   required, or the script skips (exit 0)
#   LOOM_LIVE_DAEMON_BIN          loom-daemon with `roll-pause` (default: PATH)
#   LOOM_LIVE_RUNTIMES            "claude codex" (default) or a subset
#   LOOM_LIVE_CLAUDE_MODEL        default sonnet
#   LOOM_LIVE_CODEX_PROFILE       a session-managed CODEX_HOME profile dir
#                                 (default ~/.loom/codex-profiles/agent-3)
#   LOOM_LIVE_CONTAINER_HOOK_BIN  pause-hook binary as seen INSIDE the session
#                                 container, when the image's own loom-daemon
#                                 predates `roll-pause` (forwarded as
#                                 LOOM_ROLL_PAUSE_BIN)
#   LOOM_LIVE_WORKDIR             scratch root; must be bind-mounted into the
#                                 session container at the same path
#                                 (default <repo>/.loom/state/roll-pause-live)
#   LOOM_LIVE_SKIP_EXTRAS=1       skip the hook-timeout and subagent probes
#
# Exit status: 0 when every gated check passed, 1 otherwise. Every process and
# file the test starts is cleaned up on exit, including container residue.
set -uo pipefail

if [[ "${LOOM_LIVE_ROLL_PAUSE_TEST:-}" != "1" ]]; then
    echo "SKIP: live test (set LOOM_LIVE_ROLL_PAUSE_TEST=1; needs real Claude and Codex credentials)"
    exit 0
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
SCRIPTS="$REPO/defaults/scripts"
DAEMON="${LOOM_LIVE_DAEMON_BIN:-$(command -v loom-daemon || true)}"
RUNTIMES="${LOOM_LIVE_RUNTIMES:-claude codex}"
CLAUDE_MODEL="${LOOM_LIVE_CLAUDE_MODEL:-sonnet}"
CODEX_PROFILE="${LOOM_LIVE_CODEX_PROFILE:-$HOME/.loom/codex-profiles/agent-3}"
ROOT="${LOOM_LIVE_WORKDIR:-$REPO/.loom/state/roll-pause-live}"
RUN="$ROOT/run-$(date -u +%Y%m%dT%H%M%SZ)-$$"
FROM_V="live-from"; TO_V="live-to"
PASS=0; FAIL=0; NOTES=()
pass() { PASS=$((PASS + 1)); echo "  PASS: $*"; }
fail() { FAIL=$((FAIL + 1)); echo "  FAIL: $*"; }
note() { NOTES+=("$*"); echo "  NOTE: $*"; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }

[[ -x "$DAEMON" ]] && "$DAEMON" roll-pause --help >/dev/null 2>&1 \
    || { echo "FATAL: LOOM_LIVE_DAEMON_BIN ($DAEMON) has no 'roll-pause' subcommand"; exit 1; }
mkdir -p "$RUN"
echo "live run dir: $RUN"
echo "daemon: $("$DAEMON" --version 2>/dev/null | head -1)"

CONTAINER=""
DEV_MARKERS=()
SPAWN_PIDS=()
cleanup() {
    local p m
    for p in ${SPAWN_PIDS[@]+"${SPAWN_PIDS[@]}"}; do kill_tree "$p" >/dev/null 2>&1; done
    for m in ${DEV_MARKERS[@]+"${DEV_MARKERS[@]}"}; do
        pkill -KILL -f "$m" 2>/dev/null
        [[ -n "$CONTAINER" ]] && docker exec "$CONTAINER" pkill -KILL -f "$m" 2>/dev/null
    done
    [[ "${LOOM_LIVE_KEEP:-}" == "1" ]] || rm -rf "$RUN"
}
trap cleanup EXIT

descendants() { local c; for c in $(pgrep -P "$1" 2>/dev/null); do echo "$c"; descendants "$c"; done; }

# Freeze-then-kill a whole tree (the design's launchd teardown, §5): SIGSTOP
# first so nothing can fork or reparent in the gap, then SIGKILL.
kill_tree() { # <root pid> [argv marker of a detached child]
    local root="$1" pids
    pids="$root $(descendants "$root") $([[ -n "${2:-}" ]] && pgrep -f "$2")"
    # shellcheck disable=SC2086
    kill -STOP $pids 2>/dev/null
    # shellcheck disable=SC2086
    kill -KILL $pids 2>/dev/null
    # shellcheck disable=SC2086
    printf '%s\n' $pids | sort -un | tr '\n' ' '
}

# A child that exited stays a zombie until reaped, and `kill -0` still succeeds
# on it: ask ps for the state instead.
pid_running() { local st; st="$(ps -o stat= -p "$1" 2>/dev/null)"; [[ -n "$st" && "$st" != Z* ]]; }

wait_for() { # <secs> <cmd...>
    local deadline=$(( $(date +%s) + $1 )); shift
    until "$@"; do (( $(date +%s) >= deadline )) && return 1; sleep 1; done
}

# A scratch git repo wired like a Loom workspace: this branch's hooks in
# .loom/hooks, the Claude wiring for roll-pause.sh in .claude/settings.json
# (the same entries this repo ships), and a dummy dev-server starter.
make_repo() {
    local dir="$1"
    mkdir -p "$dir/.loom/hooks" "$dir/.claude"
    cp "$REPO"/defaults/hooks/*.sh "$dir/.loom/hooks/"
    chmod +x "$dir"/.loom/hooks/*.sh
    ln -s "$SCRIPTS" "$dir/.loom/scripts"
    echo '{}' >"$dir/.loom/config.json"
    jq '{hooks: {PreToolUse: [.hooks.PreToolUse[] | select(.matcher == "*")],
                 PostToolUse: .hooks.PostToolUse, PostToolUseFailure: .hooks.PostToolUseFailure}}' \
        "$REPO/.claude/settings.json" >"$dir/.claude/settings.json"
    cat >"$dir/start-devserver.sh" <<'EOF'
#!/usr/bin/env bash
# Dummy dev server: detaches into its own session like vite/workerd do, so a
# process-group kill cannot reach it.
M="$1"
if command -v setsid >/dev/null 2>&1; then
    setsid nohup bash -c "exec -a '$M' sleep 3600" >/dev/null 2>&1 </dev/null &
else
    nohup perl -MPOSIX -e 'POSIX::setsid(); exec {"sleep"} $ARGV[0], "3600"' "$M" >/dev/null 2>&1 </dev/null &
fi
echo "dev server started"
EOF
    chmod +x "$dir/start-devserver.sh"
    printf '.claude/\n.loom/\n' >"$dir/.gitignore"
    git -C "$dir" init -q && git -C "$dir" add -A >/dev/null \
        && git -C "$dir" -c user.email=live@test -c user.name=live commit -qm init
}

task_prompt() { # <nonce> <marker>
    cat <<EOF
The nonce for this task is $1. Remember it; you will be asked for it at the end.
Perform the following five steps strictly one at a time. Each step is exactly one
shell command, run as its own separate tool call. Never combine steps, never run
them in parallel, and do not run any other commands.
step 1: ./start-devserver.sh $2
step 2: echo one >> steps.log
step 3: sleep 6; echo two >> steps.log
step 4: echo three >> steps.log
step 5: echo four >> steps.log
After step 5, reply with the word DONE followed by the nonce.
EOF
}

steps_of() { tr '\n' ' ' <"$1/steps.log" 2>/dev/null | sed 's/ *$//'; }
has_one() { grep -qx one "$1/steps.log" 2>/dev/null; }

dev_alive() { # <marker> [container]
    if [[ -n "${2:-}" ]]; then docker exec "$2" pgrep -f "$1" >/dev/null 2>&1; else pgrep -f "$1" >/dev/null 2>&1; fi
}

# The shared middle of every case: pause after step 2 and wait for the safe
# point. The caller then stops the tree at once, while the call is still
# parked, which is the order the daemon uses (§5). Sets SAFE_POINT_JSON.
pause_and_check() { # <label> <dir> <item> <pause_root> <spawn_pid>
    local label="$1" dir="$2" item="$3" proot="$4" spid="$5" idir sp at_sp
    idir="$proot/$item"
    wait_for 240 has_one "$dir" || { fail "$label: step 2 never ran (steps.log: $(steps_of "$dir"))"; return 1; }
    "$DAEMON" roll-pause request --item "$item" --dir "$proot" --from "$FROM_V" --to "$TO_V"
    log "$label: pause requested (steps.log: $(steps_of "$dir"))"
    if ! wait_for 120 test -s "$idir/safe-point.json"; then
        fail "$label: (a) no safe point within 120 s"; return 1
    fi
    at_sp="$(steps_of "$dir")"
    sp="$(cat "$idir/safe-point.json")"; SAFE_POINT_JSON="$sp"
    log "$label: safe point $sp"
    log "$label: steps.log at the safe point: [$at_sp]"
    local parked; parked="$(jq -r .parked_summary <<<"$sp")"
    case "$at_sp" in
        "one") [[ "$parked" == *"echo two"* ]] ;;
        "one two") [[ "$parked" == *"echo three"* ]] ;;
        *) false ;;
    esac && pass "$label: (a) parked at a safe point: [$at_sp] done, parked call '$parked' has not run" \
         || fail "$label: (a) not a safe point: steps [$at_sp], parked '$parked'"
    sleep 2
    [[ "$(steps_of "$dir")" == "$at_sp" ]] && pass "$label: (a) nothing ran after the safe point" \
        || fail "$label: (a) a step ran after the safe point: [$(steps_of "$dir")]"
    pid_running "$spid" && pass "$label: the session is alive and parked at the safe point" \
        || fail "$label: the session exited before the teardown"
}

finish_checks() { # <label> <dir> <out> <nonce>
    local label="$1" dir="$2" out="$3" nonce="$4"
    grep -q "$nonce" "$out" && pass "$label: (d) recalled the nonce" || fail "$label: (d) nonce not recalled (output: $(tail -c 300 "$out"))"
    [[ "$(steps_of "$dir")" == "one two three four" ]] \
        && pass "$label: (d) parked step re-run exactly once, task completed in order: [one two three four]" \
        || fail "$label: (d) steps after resume: [$(steps_of "$dir")]"
    grep -q DONE "$out" && pass "$label: (d) task reported DONE" || fail "$label: (d) no DONE in the resumed output"
}

# ---------------------------------------------------------------------------
run_claude() {
    local dir="$RUN/claude" item="live-claude-$$" proot nonce marker sid out err spid
    echo; echo "=== Claude Code (host) ==="
    make_repo "$dir"; proot="$dir/.loom/state/roll-pause"
    nonce="NONCE-C-$RANDOM$RANDOM"; marker="b10830-devserver-claude-$$"; DEV_MARKERS+=("$marker")
    sid="$(uuidgen | tr 'A-Z' 'a-z')"; out="$RUN/claude.out"; err="$RUN/claude.err"
    (
        cd "$dir" || exit 1
        export LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item" LOOM_ROLL_PAUSE_DIR="$proot" \
            LOOM_CLAUDE_SESSION_ID="$sid" LOOM_ROLL_PAUSE_BIN="$DAEMON" LOOM_DAEMON_SELF_BIN="$DAEMON" \
            LOOM_ROLL_PAUSE_PARK_SECS=15 LOOM_SWEEP_CPU_QUOTA=0
        exec bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions \
            --use-wrapper "$(task_prompt "$nonce" "$marker")"
    ) >"$out" 2>"$err" &
    spid=$!; disown "$spid"; SPAWN_PIDS+=("$spid")
    log "claude: spawned pid $spid, pinned session $sid"
    pause_and_check claude "$dir" "$item" "$proot" "$spid" || return
    [[ "$(jq -r .session_id <<<"$SAFE_POINT_JSON")" == "$sid" ]] \
        && pass "claude: the pinned --session-id is the running session's id" \
        || fail "claude: safe point names session $(jq -r .session_id <<<"$SAFE_POINT_JSON"), pinned $sid"
    dev_alive "$marker" && pass "claude: dev server running before teardown" || fail "claude: dev server never started"
    # The agent tree and the argv-attributed dev server, frozen together (§5).
    local tree; tree="$(kill_tree "$spid" "$marker")"
    sleep 2
    local left=""; for p in $tree; do kill -0 "$p" 2>/dev/null && left+=" $p"; done
    [[ -z "$left" ]] && ! dev_alive "$marker" && pass "(b) claude: tree [$tree] and the setsid dev server are gone" \
        || fail "(b) claude: still alive:${left} dev=$(dev_alive "$marker" && echo yes || echo no)"
    rm -rf "${proot:?}/$item"
    local prompt; prompt="$("$DAEMON" roll-pause resume-prompt --from "$FROM_V" --to "$TO_V" --safe-point <(printf '%s' "$SAFE_POINT_JSON"))"
    log "claude: resuming session $sid"
    (
        cd "$dir" || exit 1
        export LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item-r1" LOOM_ROLL_PAUSE_DIR="$proot" \
            LOOM_RESUME_SESSION_ID="$sid" LOOM_RESUME_PROMPT="$prompt" LOOM_ROLL_PAUSE_BIN="$DAEMON" \
            LOOM_DAEMON_SELF_BIN="$DAEMON" LOOM_SWEEP_CPU_QUOTA=0
        exec timeout 600 bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions --use-wrapper
    ) >"$out.resume" 2>"$err.resume"
    log "claude: resumed session exited $?"
    pass "(c) claude: resumed by session id in the same cwd (account $(grep -o "LOOM_ACCOUNT name=[^ ]*" "$err.resume" | head -1), first run $(grep -o "LOOM_ACCOUNT name=[^ ]*" "$err" | head -1))"
    finish_checks claude "$dir" "$out.resume" "$nonce"
    local transcript; transcript="$(ls "$HOME"/.claude/projects/*/"$sid".jsonl 2>/dev/null | head -1)"
    note "claude: dangling call after resume: $(grep -m1 -o '\[Tool call interrupted[^]]*\]' "$transcript" 2>/dev/null || echo 'no interrupted marker found')"
    dev_alive "$marker" && note "claude: the resumed session started its dev server again" && pkill -KILL -f "$marker"
}

# ---------------------------------------------------------------------------
run_codex() {
    local dir="$RUN/codex" item="live-codex-$$" proot nonce marker out err spid handle sid
    echo; echo "=== Codex (session container) ==="
    [[ -r "$CODEX_PROFILE/.session-managed.json" ]] || { fail "codex: $CODEX_PROFILE is not session-managed"; return; }
    CONTAINER="$(jq -r .container_name "$CODEX_PROFILE/.session-managed.json")"
    make_repo "$dir"; proot="$dir/.loom/state/roll-pause"; handle="$proot/$item/handle.json"
    nonce="NONCE-X-$RANDOM$RANDOM"; marker="b10830-devserver-codex-$$"; DEV_MARKERS+=("$marker")
    out="$RUN/codex.out"; err="$RUN/codex.err"
    (
        cd "$dir" || exit 1
        export LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item" LOOM_ROLL_PAUSE_DIR="$proot" \
            LOOM_RESUME_HANDLE_FILE="$handle" LOOM_CODEX_HOME="$CODEX_PROFILE" LOOM_DAEMON_SELF_BIN="$DAEMON" \
            ${LOOM_LIVE_CONTAINER_HOOK_BIN:+LOOM_ROLL_PAUSE_BIN="$LOOM_LIVE_CONTAINER_HOOK_BIN"}
        exec bash "$SCRIPTS/spawn-codex.sh" -p "$(task_prompt "$nonce" "$marker")" --dangerously-skip-permissions
    ) >"$out" 2>"$err" &
    spid=$!; disown "$spid"; SPAWN_PIDS+=("$spid")
    log "codex: spawned pid $spid in container $CONTAINER"
    pause_and_check codex "$dir" "$item" "$proot" "$spid" || return
    if pid_running "$spid" && [[ -s "$handle" ]]; then
        pass "codex: session id captured LIVE (handle written while the session runs): $(jq -c . "$handle")"
    else
        fail "codex: no live handle (spawn alive: $(pid_running "$spid" && echo yes || echo no), handle: $(cat "$handle" 2>/dev/null))"
    fi
    sid="$(jq -r .session_id "$handle" 2>/dev/null)"
    dev_alive "$marker" "$CONTAINER" && pass "codex: dev server running in the container before teardown" \
        || fail "codex: dev server not running in the container"
    # Stop through the session-exec cancel marker (§5): TERM the spawn script.
    kill -TERM "$spid" 2>/dev/null
    wait_for 60 eval '! pid_running "$spid"' || fail "codex: spawn-codex.sh still running 60 s after TERM"
    sleep 3
    if dev_alive "$marker" "$CONTAINER"; then
        fail "(b) codex: the session-exec cancel left the setsid dev server running in $CONTAINER (reaped by the test)"
        docker exec "$CONTAINER" pkill -KILL -f "$marker" 2>/dev/null
    else
        pass "(b) codex: invocation cancelled and the setsid dev server is gone from $CONTAINER"
    fi
    docker exec "$CONTAINER" pgrep -fa "codex exec" >/dev/null 2>&1 \
        && fail "(b) codex: a codex process is still running in $CONTAINER" || pass "(b) codex: no codex process left in $CONTAINER"
    docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
        && pass "codex: the session container is still running" || fail "codex: the session container is down"
    rm -rf "${proot:?}/$item"
    local prompt; prompt="$("$DAEMON" roll-pause resume-prompt --from "$FROM_V" --to "$TO_V" --safe-point <(printf '%s' "$SAFE_POINT_JSON"))"
    log "codex: resuming session $sid (account pinned: $CODEX_PROFILE)"
    (
        cd "$dir" || exit 1
        export LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item-r1" LOOM_ROLL_PAUSE_DIR="$proot" \
            LOOM_RESUME_SESSION_ID="$sid" LOOM_RESUME_PROMPT="$prompt" LOOM_CODEX_HOME="$CODEX_PROFILE" \
            LOOM_DAEMON_SELF_BIN="$DAEMON" ${LOOM_LIVE_CONTAINER_HOOK_BIN:+LOOM_ROLL_PAUSE_BIN="$LOOM_LIVE_CONTAINER_HOOK_BIN"}
        exec timeout 600 bash "$SCRIPTS/spawn-codex.sh" --dangerously-skip-permissions
    ) >"$out.resume" 2>"$err.resume"
    log "codex: resumed session exited $?"
    grep -q "container=$CONTAINER" "$err.resume" && pass "(c) codex: resumed by session id inside $CONTAINER, same profile" \
        || fail "(c) codex: resume did not run in $CONTAINER ($(grep -m1 -o 'container=[^ ]*' "$err.resume"))"
    finish_checks codex "$dir" "$out.resume" "$nonce"
    local rollout; rollout="$(find "$CODEX_PROFILE/sessions" -name "*$sid*.jsonl" 2>/dev/null | head -1)"
    local dangling; dangling="$(jq -rs '[.[] | select(.type == "response_item") | .payload] as $p
        | ([$p[] | select(.type | test("_call$")) | .call_id] - [$p[] | select(.type | test("_call_output$")) | .call_id]) | join(",")' "$rollout" 2>/dev/null)"
    note "codex: dangling call after resume: tool call(s) [${dangling:-none}] have NO output record in the rollout ($rollout): Codex writes no synthetic result, so the resume prompt is the model's only notice that the call did not run"
}

# ---------------------------------------------------------------------------
# Recorded, not gated: what a parked call does when its park window runs out
# before the daemon stops the tree (the hook denies "paused for a daemon roll").
probe_park_expiry() { # <runtime>
    local rt="$1" dir="$RUN/expiry-$1" item="expiry-$1-$$" proot out
    echo; echo "=== $rt park-window expiry probe (recorded) ==="
    make_repo "$dir"; proot="$dir/.loom/state/roll-pause"; out="$RUN/expiry-$rt.out"
    "$DAEMON" roll-pause request --item "$item" --dir "$proot"
    local ask="Run exactly this one shell command: echo ran >> probe.log . Then reply FINISHED."
    if [[ "$rt" == claude ]]; then
        ( cd "$dir" && LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item" LOOM_ROLL_PAUSE_DIR="$proot" LOOM_ROLL_PAUSE_BIN="$DAEMON" \
            LOOM_DAEMON_SELF_BIN="$DAEMON" LOOM_ROLL_PAUSE_PARK_SECS=8 LOOM_SWEEP_CPU_QUOTA=0 \
            timeout 300 bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions "$ask" ) >"$out" 2>"$out.err"
    else
        ( cd "$dir" && env LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item" LOOM_ROLL_PAUSE_DIR="$proot" LOOM_CODEX_HOME="$CODEX_PROFILE" \
            LOOM_DAEMON_SELF_BIN="$DAEMON" ${LOOM_LIVE_CONTAINER_HOOK_BIN:+LOOM_ROLL_PAUSE_BIN="$LOOM_LIVE_CONTAINER_HOOK_BIN"} \
            timeout 300 bash "$SCRIPTS/spawn-codex.sh" -p "$ask" --dangerously-skip-permissions ) >"$out" 2>"$out.err"
    fi
    local rc=$? parked; parked="$(find "$proot/$item/parked" -type f 2>/dev/null | wc -l | tr -d ' ')"
    note "$rt park expiry: session exited $rc on its own; parked (then denied) calls: $parked; tool ran: $([[ -s "$dir/probe.log" ]] && echo YES || echo no); safe point: $([[ -s "$proot/$item/safe-point.json" ]] && echo yes || echo no); final message: $(tail -c 240 "$out" | tr '\n' ' ')"
    [[ ! -s "$dir/probe.log" ]] && pass "$rt: a denied parked call never ran" || fail "$rt: a parked call RAN after its park window"
}

# Recorded, not gated: does Claude run a tool call whose PreToolUse hook
# outlives the hook's own timeout? (Why the park window must end first.)
probe_claude_hook_timeout() {
    local dir="$RUN/claude-timeout"
    echo; echo "=== Claude hook-timeout probe (recorded) ==="
    make_repo "$dir"
    jq '.hooks.PreToolUse[0].hooks[0].timeout = 5' "$dir/.claude/settings.json" >"$dir/s.json" && mv "$dir/s.json" "$dir/.claude/settings.json"
    mkdir -p "$dir/.loom/state/roll-pause/probe-$$" && echo '{}' >"$dir/.loom/state/roll-pause/probe-$$/request"
    ( cd "$dir" && LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="probe-$$" LOOM_ROLL_PAUSE_BIN="$DAEMON" LOOM_DAEMON_SELF_BIN="$DAEMON" \
        LOOM_ROLL_PAUSE_DIR="$dir/.loom/state/roll-pause" LOOM_ROLL_PAUSE_PARK_SECS=30 LOOM_SWEEP_CPU_QUOTA=0 \
        timeout 300 bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions \
        "Run exactly this one shell command and nothing else: echo ran >> probe.log . Then reply FINISHED." ) >"$RUN/probe.out" 2>"$RUN/probe.err"
    if [[ -s "$dir/probe.log" ]]; then
        note "claude hook-timeout: a 30 s park under a 5 s hook timeout -> the tool RAN after the timeout (fail-open). The park window MUST stay below the hook timeout (default 50 s < Claude's 60 s)."
    else
        note "claude hook-timeout: a 30 s park under a 5 s hook timeout -> the tool did NOT run (output: $(tail -c 200 "$RUN/probe.out" | tr '\n' ' '))"
    fi
}

# Recorded: the cost of a safe point landing while a Task subagent is mid-task.
probe_claude_subagent() {
    local dir="$RUN/claude-subagent" item="live-sub-$$" proot sid nonce spid
    echo; echo "=== Claude Task-subagent restart cost (recorded) ==="
    make_repo "$dir"; proot="$dir/.loom/state/roll-pause"; sid="$(uuidgen | tr 'A-Z' 'a-z')"; nonce="NONCE-S-$RANDOM"
    local prompt="The nonce is $nonce; remember it. Use the Task tool exactly once to dispatch ONE general-purpose subagent with this instruction: 'Run these four shell commands one at a time, each as its own tool call: echo a >> sub.log ; sleep 5; echo b >> sub.log ; echo c >> sub.log ; echo d >> sub.log . Then report done.' Wait for it to finish, then reply DONE and the nonce."
    ( cd "$dir" && export LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item" LOOM_ROLL_PAUSE_DIR="$proot" LOOM_CLAUDE_SESSION_ID="$sid" \
        LOOM_ROLL_PAUSE_BIN="$DAEMON" LOOM_DAEMON_SELF_BIN="$DAEMON" LOOM_ROLL_PAUSE_PARK_SECS=15 LOOM_SWEEP_CPU_QUOTA=0 \
        && exec bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions "$prompt" ) >"$RUN/sub.out" 2>"$RUN/sub.err" &
    spid=$!; disown "$spid"; SPAWN_PIDS+=("$spid")
    wait_for 240 grep -sqx a "$dir/sub.log" || { note "subagent: never started (sub.log empty)"; kill_tree "$spid" >/dev/null; return; }
    "$DAEMON" roll-pause request --item "$item" --dir "$proot"
    wait_for 120 test -s "$proot/$item/safe-point.json" || { note "subagent: no safe point"; kill_tree "$spid" >/dev/null; return; }
    local before; before="$(tr '\n' ' ' <"$dir/sub.log")"
    kill_tree "$spid" >/dev/null
    local rp; rp="$("$DAEMON" roll-pause resume-prompt --from "$FROM_V" --to "$TO_V" --safe-point "$proot/$item/safe-point.json")"
    rm -rf "${proot:?}/$item"
    ( cd "$dir" && LOOM_WORKSPACE="$dir" LOOM_DAEMON_ITEM_ID="$item-r1" LOOM_ROLL_PAUSE_DIR="$proot" LOOM_RESUME_SESSION_ID="$sid" \
        LOOM_RESUME_PROMPT="$rp" LOOM_ROLL_PAUSE_BIN="$DAEMON" LOOM_DAEMON_SELF_BIN="$DAEMON" LOOM_SWEEP_CPU_QUOTA=0 \
        timeout 600 bash "$SCRIPTS/spawn-claude.sh" -p --model "$CLAUDE_MODEL" --dangerously-skip-permissions ) >"$RUN/sub.out.resume" 2>&1
    # How the resumed parent handled its dangling Task call: dispatches made
    # after the resume prompt, and any subagent step that ran twice.
    local transcript redispatched dup
    transcript="$(ls "$HOME"/.claude/projects/*/"$sid".jsonl 2>/dev/null | head -1)"
    redispatched="$(sed -n '/paused for a daemon roll/,$p' "$transcript" 2>/dev/null | jq -r 'select(.type == "assistant") | .message.content[]? | select(.type == "tool_use") | .name' 2>/dev/null | grep -cE '^(Task|Agent)$')"
    dup="$(sort "$dir/sub.log" | uniq -d | tr '\n' ' ')"
    note "subagent: sub.log at the safe point: [$before]; after resume: [$(tr '\n' ' ' <"$dir/sub.log")]; Task/Agent dispatches after the resume: ${redispatched:-0}; steps run twice: [${dup}]; nonce recalled: $(grep -q "$nonce" "$RUN/sub.out.resume" && echo yes || echo no); DONE: $(grep -q DONE "$RUN/sub.out.resume" && echo yes || echo no)"
}

for rt in $RUNTIMES; do
    case "$rt" in
        claude) run_claude ;;
        codex) run_codex ;;
        *) echo "unknown runtime $rt" ;;
    esac
done
if [[ "${LOOM_LIVE_SKIP_EXTRAS:-}" != "1" ]]; then
    for rt in $RUNTIMES; do probe_park_expiry "$rt"; done
    if [[ " $RUNTIMES " == *" claude "* ]]; then
        probe_claude_hook_timeout
        probe_claude_subagent
    fi
fi

echo
echo "=== Recorded observations ==="
for n in ${NOTES[@]+"${NOTES[@]}"}; do echo "  - $n"; done
echo
echo "test-roll-pause-resume-live.sh: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
