#!/usr/bin/env bash
# reap-process-group.sh — best-effort self-reap of a script's own children at
# its own exit (success, failure, OR signal), Issue #6192.
#
# Why: a wedged-disk incident (2026-08-14) left `cargo build` children and a
# pipe-holding `tail -100` helper alive as orphans re-parented to launchd,
# invisible to anything not specifically hunting for them, well past the
# lifetime of the sweep that spawned them. The daemon's own #4980/#3800
# mechanism already reaps a DEAD sweep leader's surviving process group — but
# only once the daemon's reaper notices the leader died, and only for
# daemon-tracked sweeps. This is the complementary, sweep-SIDE backstop: it
# fires on the wrapping process's OWN exit, independent of whether a daemon is
# watching at all, so it also covers a manually-run wrapper.
#
# Bound to the process's own exit, never the daemon's: this function does
# nothing until IT runs (at EXIT), so a daemon restart — which never touches
# this already-running process tree — cannot trigger it. This is deliberate:
# sweeps must keep surviving daemon restarts (documented design), and this
# mechanism is scoped so it cannot interfere with that.
#
# SAFETY-CRITICAL scope split (do not "simplify" this away): a process only
# ever unconditionally OWNS its own descendant subtree. Its process GROUP is a
# different, broader thing — every process inherits its parent's pgid unless
# something explicitly changes it, so a caller that is merely a foreground
# child of some larger session (a `bash .loom/scripts/build-gate.sh` run from
# inside a live Claude Bash tool call, say) shares its pgid with siblings it
# has no relationship to at all (the agent session itself, other concurrent
# tool invocations, …). Blindly signalling "everyone else in my pgid" from
# such a caller would be able to kill unrelated, still-useful work. So:
#
#   - When the caller IS its own process-group leader (pgid == its own pid —
#     true for a sweep's actual leader process, since the daemon spawns it
#     with `process_group(0)`/`setpgid(0,0)`), the ENTIRE group is exclusively
#     that sweep's own tree, and is reaped in full — this catches grandchild
#     processes a child forgot to clean up before it exited too.
#   - Otherwise (the caller is a plain foreground child inheriting someone
#     else's group — e.g. `build-gate.sh` run as one Bash-tool step of a
#     larger session), only the caller's own DIRECT DESCENDANT SUBTREE is
#     reaped (recursive `pgrep -P`, depth-first) — never anything outside it,
#     regardless of shared pgid.
#
# Source this file (do not exec). Defines:
#
#   loom_reap_own_process_group [label]
#     Best-effort TERM-then-KILL of either the caller's whole process group
#     (leader case) or its own descendant subtree (non-leader case) — see the
#     split above. Never touches the caller's own PID. Safe to call from an
#     `EXIT` trap; every step is best-effort and the function always
#     returns 0.
#
# Opt-out: LOOM_SWEEP_SELF_REAP=0 makes this a no-op (restores pre-#6192
# behavior for any repo/operator that hits a regression), mirroring the
# existing master-disable convention (LOOM_SWEEP_NICE=0, LOOM_BUILD_GATE_NICE=0).

# Recursively collect all descendant PIDs of a given PID (depth-first,
# bottom-up order so a killer can TERM leaves before their parents).
# Mirrors kill-session-tree.sh's `_collect_descendants`.
_loom_reap_collect_descendants() {
    local parent_pid="$1"
    local children
    children="$(pgrep -P "$parent_pid" 2>/dev/null || true)"
    local child
    for child in $children; do
        _loom_reap_collect_descendants "$child"
        echo "$child"
    done
}

loom_reap_own_process_group() {
    local label="${1:-process-group}"

    if [[ "${LOOM_SWEEP_SELF_REAP:-1}" == "0" ]]; then
        return 0
    fi

    local my_pid="$$"
    local my_pgid
    my_pgid="$(ps -o pgid= -p "$my_pid" 2>/dev/null | tr -d ' ')"

    local members scope
    if [[ -n "$my_pgid" && "$my_pgid" == "$my_pid" ]]; then
        # Leader case: this process's group is exclusively its own tree.
        members="$(pgrep -g "$my_pgid" 2>/dev/null | grep -v -x "$my_pid" || true)"
        scope="process group ${my_pgid}"
    else
        # Non-leader case: only reap what THIS process itself spawned —
        # never anything else sharing its (inherited) group.
        members="$(_loom_reap_collect_descendants "$my_pid")"
        scope="descendant subtree of pid ${my_pid}"
    fi
    members="$(printf '%s\n' "$members" | sed '/^$/d' | sort -u)"
    [[ -z "$members" ]] && return 0

    local count
    count="$(printf '%s\n' "$members" | wc -l | tr -d ' ')"
    [[ "$count" -eq 0 ]] && return 0

    echo "[${label}] reaping ${count} residual process(es) in ${scope} at exit (#6192): ${members//$'\n'/ }" >&2
    # shellcheck disable=SC2086
    kill -TERM $members 2>/dev/null || true
    sleep 2
    local pid
    for pid in $members; do
        if kill -0 "$pid" 2>/dev/null; then
            kill -KILL "$pid" 2>/dev/null || true
        fi
    done
    return 0
}
