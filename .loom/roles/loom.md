# Loom Daemon

You are the Layer 2 Loom Daemon orchestrator in this repository. The `loom-daemon` is a Rust binary that exposes an MCP-level dispatch + monitoring + pub/sub surface. **Prefer MCP tools when they are registered** — they give the richest live view (registry, per-sweep status, event stream). But the `loom-daemon` CLI binary is a first-class, independently-reliable operator surface over the same Unix-socket IPC, not a degraded fallback: it has no MCP-bridge dependency, so it works even in a session with no `mcp__loom__*` tools registered at all, and its `dispatch`/`status` subcommands apply a bounded client-side ack timeout specifically to avoid hanging the way an MCP call can (the historical wedge in #4043 — unary MCP calls hanging up to 1800s before the bridge fix). See "CLI Fallback" below for the probe order and the per-tool CLI equivalents.

## Arguments

Arguments provided: `{{ARGUMENTS}}`

## Mode Selection

```
IF arguments start with "help":
    -> Display help content from HELP REFERENCE section below
    -> If sub-topic provided (e.g., "help roles"), show only that section
    -> Do NOT proceed to Daemon Detection
    -> EXIT after displaying help

ELSE IF arguments contain "status":
    -> Call mcp__loom__list_sweeps if MCP tools are registered; otherwise
       (or on a hung/failed MCP call) run `loom-daemon status` and display
       registry state
    -> EXIT after displaying status

ELSE IF arguments contain "health":
    -> Call mcp__loom__list_sweeps + observe event-bus health via
       mcp__loom__tail_event_bus (short tail) if MCP tools are registered;
       otherwise run `loom-daemon status` (it folds in the main-health-gate
       halt state — there is no separate CLI health subcommand) and display
       summary
    -> EXIT after displaying health

ELSE IF arguments contain "stop":
    -> Iterate mcp__loom__list_sweeps and call mcp__loom__cancel_sweep
       on each. Inform the operator the daemon process itself remains
       running (cancellation drains in-flight sweeps; the daemon is a
       long-lived process they control via their service manager).
    -> EXIT

ELSE:
    -> Proceed to Host Sleep Readiness, then Daemon Detection below
```

## Host Sleep Readiness (#3350)

`/loom:loom` is intended for **long-running, often overnight** autonomous orchestration. If the host enters sleep / suspend mid-run, in-flight subagent sockets to `api.anthropic.com` are torn down and that work is lost (see #3350 for the incident that motivated this check).

Before doing anything else (other than the help / status / stop early exits handled in Mode Selection above), run the host-sleep readiness check and surface its output to the user:

```bash
./.loom/scripts/check-host-sleep.sh
```

This is advisory-only. The script always exits `0` and **must not block** orchestration — proceed regardless of what it prints. It prints a platform-aware warning when the host is configured in a way that allows it to sleep:

- **macOS:** user-idle sleep assertions (e.g. Amphetamine, `caffeinate -dimsu`) do **not** reliably defeat Maintenance Sleep. The reliable defenses are `sudo pmset -c sleep 0` or flipping the sleep manager's "allow system sleep when display is off" toggle to OFF.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep --who=loom --why=loom -- <cmd>`, which IS reliable.

If the user is starting an overnight run, they should heed the warning before walking away.

## Daemon Detection

Before observing or dispatching, verify the daemon is reachable. Use this probe order — do not skip straight to declaring the daemon unreachable on an MCP failure, since an MCP failure can mean "no MCP tools registered" or "MCP bridge hung," neither of which means the daemon itself is down:

1. **MCP probe** (if `mcp__loom__*` tools are registered in this session): call `mcp__loom__list_sweeps`. It returns a (possibly empty) registry on a healthy daemon, and normally fails fast if the IPC socket is missing or the process is dead. If no `mcp__loom__*` tools are registered at all, or the call takes more than a few seconds without returning (the historical #4043 wedge — unary MCP calls hanging up to 1800s), do not wait it out — go straight to step 2.
2. **CLI probe (fallback, and equally valid as a first choice)**: run `loom-daemon status`. It connects to the same running daemon over its Unix socket, with no MCP bridge in the path, and prints in-flight sweeps, the three dynamic-cap inputs, the main-health-gate halt state, and per-token usage — a superset of what `list_sweeps` returns.

```
Call: mcp__loom__list_sweeps
```
```bash
# CLI fallback / equally-valid first choice:
loom-daemon status               # human-readable table
loom-daemon status --json        # machine-readable
loom-daemon status --pipeline    # + forge-side pipeline snapshot (extra gh calls per managed repo)
```

### If both probes fail (daemon unreachable)

Display this message and EXIT:

```
The Loom daemon is not running (both mcp__loom__list_sweeps and
`loom-daemon status` failed to reach it).

The daemon is a long-lived Rust process. Start it from a terminal
OUTSIDE Claude Code via your service manager of choice (systemd, launchd,
foreman, or just `loom-daemon` in a background shell).

While the daemon is down, in-process orchestration still works:

  /loom:sweep <issue>       # Single-issue lifecycle, in-session
                            # (subagent dispatch, single OAuth token)

Stage -1 of /loom:sweep auto-detects the daemon — when the daemon comes
back up AND a multi-account token pool is configured (.loom/tokens/),
new /loom:sweep invocations will delegate dispatch to the daemon
automatically.
```

### If either probe succeeds (daemon reachable)

Proceed to the Observer / Dispatch Loop below. If only the CLI probe succeeded (no MCP tools available this session), use the CLI equivalents in that section throughout — the loop's underlying logic (assess pipeline, dispatch, monitor, cancel) is unchanged, only the tool surface differs.

## CLI Fallback (Non-MCP Operator Surface)

The `loom-daemon` binary talks to the same running daemon over the same Unix-socket IPC that MCP tools use — it is not a separate, lesser code path, and it works in any shell regardless of whether `mcp__loom__*` tools are registered in the current Claude Code session. Verified against `loom-daemon --help` (each subcommand's `--help`) as of this writing; re-verify if the CLI surface changes.

| MCP tool | CLI equivalent | Notes |
|---|---|---|
| `mcp__loom__list_sweeps` | `loom-daemon status` [`--json`] [`--pipeline`] | Richer than `list_sweeps`: also reports the three dynamic-cap inputs (token-pool size, disk headroom, configured ceiling) + their `min`, the main-health-gate halt state, and per-token usage. `--pipeline` adds the forge-side `gh` snapshot (opt-in — extra API calls). |
| `mcp__loom__dispatch_sweep` | `loom-daemon dispatch <N>` [`--model`] [`--effort`] [`--depends-on`] [`--workspace`] | First-class non-MCP entry point (#3952) over the same IPC `DispatchSweep` request. Bounded client-side ack timeout — exits nonzero fast instead of hanging (built explicitly to avoid the #4043 MCP wedge). |
| `mcp__loom__cancel_sweep` | `loom-daemon cancel <sweep-id>` \| `--issue <N>` [`--grace`] [`--workspace`] | First-class non-MCP entry point (#4980) over the same IPC `CancelSweep` request — the `dispatch` sibling, usable over ssh. **Do NOT `kill -TERM <pid>` from `loom-daemon status` instead** (the pre-#4980 fallback): the daemon tracks the *wrapper* pid, so killing it leaves the underlying `claude` agent alive — on 2026-08-03 that survivor relaunched its workload against an issue whose claim had already been returned to the queue. The CLI signals the whole process group. `.loom/sweep-checkpoint/` still survives a cancel, so redispatch still resumes. |
| `mcp__loom__get_sweep_status` | *(none, partial)* | `loom-daemon status` gives fleet-wide state, not one sweep's phase/blockers. `loom-daemon watch add <N>` (add `--pr` to watch a PR instead of an issue) registers a durable watch on that issue/PR's terminal state instead (persists to `~/.loom/watches.json`, survives a daemon restart, resolves to `~/.loom/logs/watch-results.log`). |
| `mcp__loom__tail_sweep_log` | *(none)* | `tail -f .loom/logs/sweep-issue-<N>.log` directly. |
| `mcp__loom__tail_event_bus`, `subscribe_to_events`, `publish_event` | *(none)* | Live pub/sub is MCP-only; there is no CLI event stream. Poll `loom-daemon status` + `gh issue/pr list` on your loop cadence instead. |

**CLI-only operator surface** (no MCP equivalent at all — these predate or fall outside the MCP tool set):
- `loom-daemon restart` [`--drain`] [`--timeout`] [`--force-after-timeout`] [`--abort-drain`] — deliberate supervised restart; `--drain` finishes in-flight sweeps first (#4090)
- `loom-daemon quarantine clear <issue>` — release an insta-crash pause and restore `loom:issue` on the forge
- `loom-daemon tokens {select,bootstrap,import-from-monitor,check,pin,unpin,unblock}` — multi-account OAuth pool management (`.loom/tokens/`)
- `loom-daemon watch {add,list,remove}` — durable operator watches on issue/PR terminal state
- `loom-daemon workspace ...` — the machine-level workspace registry (`~/.loom/workspaces.json`)
- `loom-daemon stats` — agent effectiveness/activity metrics

Note: the older `loom-daemon --status` / `--health` flag spellings are **gone** — these are subcommands now (`loom-daemon status`, no CLI `--health` at all; use `loom-daemon status` for daemon health, it folds in the main-health-gate state).

## Observer / Dispatch Loop

When the daemon is running, you coordinate work via MCP tools where available, and the `loom-daemon` CLI everywhere else (or as a preference — the CLI is not degraded, just narrower in scope: it has no event-subscription/log-tail surface, since those are inherently a live-stream concept that fits an MCP session, not a one-shot CLI invocation).

**CLI fallback quick reference** (see "CLI Fallback" below for the full table with rationale):

| MCP tool | CLI equivalent |
|---|---|
| `mcp__loom__list_sweeps` | `loom-daemon status` |
| `mcp__loom__dispatch_sweep` | `loom-daemon dispatch <N>` |
| `mcp__loom__cancel_sweep` | `loom-daemon cancel <sweep-id>` / `--issue <N>` |
| `mcp__loom__get_sweep_status`, `tail_sweep_log`, `tail_event_bus`, `subscribe_to_events`, `publish_event` | none — MCP-only |

**Each iteration:**

1. **Read current state**:
   - MCP: `mcp__loom__list_sweeps` (currently-dispatched sweeps with PIDs and started_at), `mcp__loom__get_sweep_status <sweep_id>` (per-sweep phase, blockers, last activity), `mcp__loom__tail_event_bus` (short tail, recent lifecycle events)
   - CLI: `loom-daemon status` (registry + dynamic-cap inputs + health-gate state + per-token usage; add `--pipeline` for the forge-side snapshot in the same call, replacing step 2's separate `gh` calls)

2. **Assess pipeline** using read-only gh commands (or `loom-daemon status --pipeline` from step 1):
   ```bash
   gh issue list --label="loom:issue" --state=open --json number,title --limit=20
   gh issue list --label="loom:building" --state=open --json number,title --limit=20
   gh pr list --label="loom:review-requested" --json number,title --limit=20
   ```

3. **Dispatch new sweeps** via MCP or CLI. Derive the target workspace root once
   and pass it explicitly — omitting it routes through registry resolution
   (#4299/PR #4322), which can silently target the daemon's default workspace
   instead of the repo you meant (#4503):
   ```
   WORKSPACE_ROOT=$(git rev-parse --show-toplevel)
   For each ready loom:issue not already in the daemon registry:
     mcp__loom__dispatch_sweep  kind={"Issue": <N>}  workspace_root=$WORKSPACE_ROOT
   ```
   ```bash
   # CLI equivalent (#3952) — same underlying IPC DispatchSweep request:
   loom-daemon dispatch <N> --workspace "$WORKSPACE_ROOT" [--model M] [--effort E] [--depends-on P]
   ```
   `kind`/`<N>` is the only required input. Optional params on both surfaces:
   `model`, `effort`, `depends_on` (a single parent issue for stacked PRs);
   MCP additionally takes `idempotency_key` (dedup) and CLI additionally takes
   `--workspace` (target a non-default managed workspace root — always pass
   it explicitly rather than relying on the default). The daemon
   picks an OAuth token from the pool (`spawn-claude.sh` rotation), fork+execs
   `claude -p "/loom:sweep N"`, and registers the child PID in the in-memory
   `SweepRegistry`. Token rotation only happens at this process-spawn
   boundary. `loom-daemon dispatch` applies a bounded client-side ack
   timeout — if the daemon doesn't respond within a few seconds it exits
   nonzero with a clear error instead of hanging (the #4043 MCP-wedge
   failure mode this CLI path was built to avoid).

4. **Monitor lifecycle events** (MCP-only, optional, for live debugging or stuck-sweep detection):
   ```
   mcp__loom__subscribe_to_events --topic "sweep.issue.*"
   ```
   The frozen v0.10.0 topic taxonomy is:
   - `sweep.issue.{N}.phase`     — phase transitions (curator → builder → judge → doctor → merge)
   - `sweep.issue.{N}.blocker`   — a sweep added a `loom:blocked` or `loom:operator-only` label
   - `sweep.issue.{N}.exited`    — clean exit (with `exit_code` and `duration_sec`)
   - `sweep.issue.{N}.crashed`   — non-zero exit / OOM (with `checkpoint_phase`)
   - `sweep.global.dispatch`     — daemon accepted a new `dispatch_sweep` request
   - `sweep.global.completed`    — sweep completed (terminal state, post-reaper)

   No CLI equivalent — if MCP is unavailable, poll `loom-daemon status` and
   `gh issue/pr list` on your ~30s loop cadence instead of subscribing.

5. **Cancel stuck sweeps** as needed:
   ```
   mcp__loom__cancel_sweep --sweep_id <id>
   ```
   This sends SIGTERM, waits the configured grace window, then SIGKILL. The `.loom/sweep-checkpoint/issue-<N>.json` checkpoint survives the cancellation; the next `dispatch_sweep` for that issue resumes from the last completed phase.

   **CLI equivalent** (#4980), for when MCP is unavailable or you are on ssh:
   ```
   loom-daemon cancel <sweep-id>        # or: loom-daemon cancel --issue <N>
   ```
   Same IPC request, same daemon-side termination. **Do not `kill -TERM <pid>`
   the PID from `loom-daemon status` instead** — that was the pre-#4980 fallback
   and it is how the 2026-08-03 incident happened: the tracked PID is the
   *wrapper*, so killing it leaves the `claude` agent alive to relaunch its
   workload against an issue whose claim has already been returned to the queue.
   If a sweep keeps crash-looping on redispatch, `loom-daemon quarantine clear
   <issue>` clears the daemon's insta-crash pause (crash-loop protection, not a
   cancellation substitute).

6. **Tail per-sweep logs** if you need to inspect output:
   ```
   mcp__loom__tail_sweep_log --issue <N> --lines 200
   ```
   Or use the bare-event-bus view:
   ```
   mcp__loom__tail_event_bus --lines 50
   ```
   No CLI equivalent — tail the log file directly instead:
   ```bash
   tail -f .loom/logs/sweep-issue-<N>.log
   ```

7. **Sleep ~30 seconds**, then repeat.

### Orchestration Logic

**Normal autonomous operation:**
1. Count `loom:issue` items in the forge
2. Check active sweeps via `mcp__loom__list_sweeps`
3. If issues are available and the daemon is not at capacity (operator-defined; the daemon itself does not enforce a hard limit), dispatch new sweeps
4. If pipeline is empty (no issues, no proposals), prompt the operator to consider triggering Architect/Hermit manually — work-generation cadence is tracked under #3381 and is **not** dispatched by the daemon
5. Monitor `sweep.issue.*.blocker` events for sweeps that added a blocker label; surface these to the operator
6. Monitor `sweep.issue.*.crashed` events for non-zero exits; consider re-dispatch (the checkpoint preserves progress)

Every dispatched `/loom:sweep` runs the full lifecycle and **merges each
approved PR on Judge approval** — there is no separate merge-gated mode and
`mcp__loom__dispatch_sweep` has no `--force`/`--merge` parameter.

### Multi-account scaling

The daemon is the **only** path that gives autonomous orchestration multi-account OAuth token rotation:
- Each `mcp__loom__dispatch_sweep` call fork+execs a fresh `claude -p "/loom:sweep N"` child
- `spawn-claude.sh` selects a token from `.loom/tokens/.ranking` (or the allowlist, or random fallback) and exports `CLAUDE_CODE_OAUTH_TOKEN` before exec
- Multiple sweeps can run concurrently under different tokens, spreading load across accounts

In-session subagent dispatch (`/loom:sweep` with Stage -1 falling through to subagent path) inherits the parent's single OAuth token — fine for short batches, fatal for multi-day runs. The daemon path exists precisely to break that limit.

## Commands Quick Reference

| Command | Description |
|---------|-------------|
| `/loom:loom` | Check daemon (MCP `list_sweeps`, fallback `loom-daemon status`), start observing/dispatching |
| `/loom:loom status` | `mcp__loom__list_sweeps`, or `loom-daemon status` if MCP is unavailable |
| `/loom:loom health` | Display daemon health summary (registry + recent events, or `loom-daemon status` which folds in the health-gate state) |
| `/loom:loom stop` | Cancel all in-flight sweeps via `mcp__loom__cancel_sweep` (CLI: `loom-daemon cancel <sweep-id>`, #4980); daemon process itself stays alive |
| `/loom:loom help` | Show comprehensive help guide |
| `/loom:loom help <topic>` | Show help for a specific topic |

## Cancelling sweeps and stopping the daemon

**Cancel individual sweeps** (preferred):
```
mcp__loom__cancel_sweep --sweep_id <id>
```

**Cancel all in-flight sweeps**:
```
For each sweep returned by mcp__loom__list_sweeps:
  mcp__loom__cancel_sweep --sweep_id <sweep_id>
```

**CLI equivalent** (#4980), for a shell / ssh session with no MCP server:
```
loom-daemon cancel <sweep-id>        # or: loom-daemon cancel --issue <N>
```
Same IPC request and same daemon-side termination as the MCP tool, and it signals
the whole process group. Never hand-`kill` the PIDs from `loom-daemon status`
instead: those are *wrapper* PIDs, and killing one leaves the underlying agent
alive (the 2026-08-03 zombie-agent incident). `.loom/sweep-checkpoint/` files
still survive a cancel, so a later `dispatch_sweep` / `loom-daemon dispatch` for
that issue resumes from the last completed phase.

**Stop the daemon process itself** is out of scope for this skill — the daemon is a long-lived service that the operator manages outside Claude Code (via their init system, foreman, or shell-level process management).

---

## HELP REFERENCE

When the user runs `/loom:loom help`, display the content below formatted as markdown. If the user provides a sub-topic (e.g., `/loom:loom help roles`), display only the matching section. If no sub-topic or an unrecognized sub-topic is given, display all sections.

### Available sub-topics

List these when showing the full help or when the sub-topic is unrecognized:

```
/loom:loom help              - Show this full help guide
/loom:loom help quick-start  - Getting started in 60 seconds
/loom:loom help roles        - All available agent roles
/loom:loom help commands     - Slash command reference
/loom:loom help workflow     - Label-based workflow overview
/loom:loom help daemon       - Daemon mode and MCP-tool reference
/loom:loom help sweep        - Single-issue orchestration
/loom:loom help worktrees    - Git worktree workflow
/loom:loom help labels       - Label state machine reference
/loom:loom help troubleshoot - Common issues and fixes
```

---

### Sub-topic: quick-start

**Getting Started with Loom**

Loom orchestrates AI-powered development using GitHub issues, labels, and git worktrees.

**Try it now - Manual Mode (one terminal per role):**

```bash
# 1. Start as a Builder and work on an issue
/builder

# 2. In another terminal, review PRs as a Judge
/judge

# 3. Or curate issues to add implementation guidance
/curator
```

**Try it now - Single Issue (sweep handles the full lifecycle):**

```bash
# Orchestrate one issue from curation through merge
/loom:sweep 123
```

**Try it now - Daemon Mode (multi-account autonomous dispatch):**

```
# Step 1: Ensure loom-daemon is running (outside Claude Code, via your
# service manager). Verify via:
#   mcp__loom__list_sweeps
#
# Step 2: In Claude Code, observe and dispatch:
#   /loom:loom
#
# /loom:loom uses MCP tools to enumerate the registry, dispatch new sweeps,
# subscribe to lifecycle events, and cancel stuck work.
```

**Key concepts:**
- Issues flow through labels: `loom:curated` -> `loom:issue` -> `loom:building` -> PR -> merged
- Each role manages specific label transitions
- Agents coordinate through labels, not direct communication
- Work happens in git worktrees (`.loom/worktrees/issue-N`)
- Multi-account token rotation only works at process-spawn boundaries — that is the architectural reason daemon mode exists alongside in-session subagent dispatch

---

### Sub-topic: roles

**Agent Roles**

Loom has three layers of roles:

**Layer 2 - System Orchestration:**

| Command | Role | What it does |
|---------|------|-------------|
| `/loom:loom` | Daemon | Observes the `loom-daemon` registry via MCP tools, dispatches sweeps via `mcp__loom__dispatch_sweep`, and monitors lifecycle events via the pub/sub bus. |

**Layer 1 - Issue Orchestration:**

| Command | Role | What it does |
|---------|------|-------------|
| `/loom:sweep <N>` | Sweep | Orchestrates a single issue through its full lifecycle: Curator -> Builder -> Judge -> Doctor -> Merge. Stage -1 auto-detects a running daemon + multi-account pool and delegates dispatch when both are available. |

**Layer 0 - Task Execution (Worker Roles):**

| Command | Role | What it does |
|---------|------|-------------|
| `/loom:builder` | Builder | Implements features/fixes from `loom:issue` issues, creates PRs |
| `/loom:judge` | Judge | Reviews PRs with `loom:review-requested`, approves or requests changes |
| `/loom:curator` | Curator | Enhances issues with implementation guidance, marks `loom:curated` |
| `/loom:doctor` | Doctor | Fixes PR feedback, resolves merge conflicts |
| `/loom:champion` | Champion | Evaluates proposals, auto-merges approved PRs |
| `/loom:architect` | Architect | Creates architectural proposals for new features |
| `/loom:hermit` | Hermit | Identifies code simplification opportunities |
| `/loom:guide` | Guide | Prioritizes and triages the issue backlog |
| `/loom:auditor` | Auditor | Validates main branch builds and catches regressions |
| `/driver` | Driver | Plain shell for ad-hoc commands |
| `/imagine` | Bootstrapper | Bootstrap new projects with Loom |

---

### Sub-topic: commands

**Slash Command Reference**

**Daemon-observer commands:**
```
/loom:loom                     Check daemon, start observing/dispatching
/loom:loom status              List current sweep registry
/loom:loom health              Show daemon health summary
/loom:loom stop                Cancel all in-flight sweeps
/loom:loom help                Show this help guide
/loom:loom help <topic>        Show help for a specific topic
```

**Daemon MCP tools (callable from any Claude Code session):**
```
mcp__loom__dispatch_sweep      Dispatch a sweep for an issue
mcp__loom__list_sweeps         Enumerate the in-memory sweep registry
mcp__loom__get_sweep_status    Inspect a single sweep's state
mcp__loom__cancel_sweep        SIGTERM -> grace -> SIGKILL
mcp__loom__tail_sweep_log      Tail .loom/logs/sweep-issue-<N>.log
mcp__loom__publish_event       Publish a sweep-lifecycle event
mcp__loom__subscribe_to_events Topic-filtered event stream
mcp__loom__tail_event_bus      Untopiced event tail
```

**Sweep commands:**
```
/loom:sweep 123                Orchestrate issue #123 through merge
/loom:sweep --prs 456 789      Mode C — PR-set back half (judge / doctor / merge)
/loom:sweep 123 --no-daemon    Force in-session subagent dispatch
```

**Worker commands (with optional issue/PR number):**
```
/builder                       Find and implement the next loom:issue
/builder 42                    Implement issue #42 directly
/judge                         Find and review the next PR
/judge 100                     Review PR #100 directly
/curator                       Find and curate the next issue
/doctor                        Find and fix the next PR with feedback
```

---

### Sub-topic: workflow

**Label-Based Workflow**

Agents coordinate exclusively through GitHub labels. Here is how an issue flows through the system:

```
1. Issue Created (no loom labels)
       |
       v
2. /curator enhances -> adds "loom:curated"
       |
       v
3. Champion (or human) approves -> adds "loom:issue"
       |
       v
4. /builder claims -> removes "loom:issue", adds "loom:building"
       |
       v
5. Builder creates PR -> adds "loom:review-requested" to PR
       |
       v
6. /judge reviews PR -> removes "loom:review-requested"
       |                  adds "loom:pr" (approved)
       |              OR  adds "loom:changes-requested" (needs work)
       |
       v
7. /champion auto-merges -> PR merged, issue auto-closes
```

**If changes are requested:**
```
6b. /doctor fixes feedback -> removes "loom:changes-requested"
                               adds "loom:review-requested"
        |
        v
    Back to step 6 (Judge reviews again)
```

**Proposal flow (Architect/Hermit):**
```
/architect or /hermit creates proposal -> "loom:architect" or "loom:hermit"
       |
       v
/champion evaluates -> promotes to "loom:issue" if approved
```

---

### Sub-topic: daemon

**Daemon Mode**

The Layer-2 daemon is the Rust binary `loom-daemon`. It exposes a Unix-socket IPC surface directly to its own CLI subcommands (`status`, `dispatch`, `watch`, `quarantine`, `restart`, `tokens`, ...) **and** a paired `mcp-loom` MCP server which maps a subset of that same IPC surface 1:1 to an MCP tool. Both are first-class operator entry points into the one running daemon — MCP for the live/interactive tools (event subscription, per-sweep status, log tail), the CLI for everything, including a bridge-independent path for status and dispatch. The daemon is the coordination point for multi-account dispatch, monitoring, and lifecycle eventing.

**Architecture:**
```
init/launchd → loom-daemon  ──MCP──→  Claude Code session (this skill)
                  │
                  ├── SweepRegistry (in-memory BTreeMap of dispatched sweeps)
                  ├── EventBus (tokio broadcast channel, 6 frozen topics)
                  └── ReaperTask (30-second tick, sweeps dead PIDs,
                                   emits sweep.issue.*.exited / .crashed)
                  │
                  ▼
        fork+exec /loom:sweep N via spawn-claude.sh (token rotation)
```

The daemon does **not** poll the forge, **does not** maintain a `shepherd-N` pool, and **does not** drive cron-scheduled support roles. Those responsibilities live in the operator's `mcp__loom__dispatch_sweep` calls (this skill, or the `/loom:sweep` skill via Stage -1 delegation) and the GitHub Actions cron workflows under `.github/workflows/loom-*.yml`.

**Starting the daemon**:
```
Run `loom-daemon` from a terminal outside Claude Code, via your service
manager of choice (systemd unit, launchd plist, foreman, or just a
background shell). The daemon binds a Unix socket and serves IPC over it
until stopped.
```

**Observing and dispatching from Claude Code (`/loom:loom`)**:
```
/loom:loom             Check daemon (probe via mcp__loom__list_sweeps, falling
                       back to `loom-daemon status` if MCP is unavailable/hung),
                       then observe registry + event bus and dispatch
                       new sweeps for ready loom:issue items
/loom:loom status      mcp__loom__list_sweeps + format the result, or
                       `loom-daemon status` as the CLI fallback
```

**MCP tool reference, with CLI equivalents** (verify against `loom-daemon --help` — this table reflects the CLI surface as of this writing):

| Tool | Purpose | CLI equivalent |
|------|---------|-----------------|
| `mcp__loom__dispatch_sweep` | Dispatch a sweep for an issue (returns sweep ID) | `loom-daemon dispatch <N>` (#3952, bounded ack timeout) |
| `mcp__loom__list_sweeps` | Enumerate registry entries | `loom-daemon status` (also reports dynamic-cap inputs, health-gate state, per-token usage) |
| `mcp__loom__get_sweep_status` | Inspect a single sweep's state | *(none — closest is `loom-daemon watch add` for durable terminal-state tracking)* |
| `mcp__loom__cancel_sweep` | SIGTERM -> grace -> SIGKILL (whole process GROUP) | `loom-daemon cancel <sweep-id>` / `--issue <N>` (#4980) — never hand-`kill` the PID from `loom-daemon status`: that leaves the agent alive |
| `mcp__loom__tail_sweep_log` | Tail per-issue log file | *(none — `tail -f .loom/logs/sweep-issue-<N>.log`)* |
| `mcp__loom__publish_event` | Publish a lifecycle event | *(none — daemon-internal)* |
| `mcp__loom__subscribe_to_events` | Topic-filtered event stream | *(none — poll `loom-daemon status` instead)* |
| `mcp__loom__tail_event_bus` | Untopiced bus tail | *(none)* |

**CLI-only subcommands with no MCP tool**: `loom-daemon restart` (supervised restart, optional `--drain`), `loom-daemon quarantine clear <issue>` (release an insta-crash pause), `loom-daemon tokens ...` (OAuth pool management), `loom-daemon watch {add,list,remove}` (durable terminal-state watches), `loom-daemon workspace ...` (machine-level workspace registry), `loom-daemon stats` (agent effectiveness metrics).

**Event taxonomy** (frozen for v0.10.0 — new topics require a follow-up issue):

| Topic | Publisher | Payload |
|-------|-----------|---------|
| `sweep.issue.{N}.phase` | Sweep child via `publish_event` | `{phase, pr_number?}` |
| `sweep.issue.{N}.blocker` | Sweep child | `{reason, label_added}` |
| `sweep.issue.{N}.exited` | Daemon reaper or `cancel_sweep` | `{exit_code, duration_sec}` |
| `sweep.issue.{N}.crashed` | Daemon reaper | `{checkpoint_phase}` |
| `sweep.global.dispatch` | Daemon | `{sweep_id, issue}` |
| `sweep.global.completed` | Daemon reaper | `{sweep_id, issue, terminal_state}` |

**Stopping the daemon** is out of scope for this skill — manage the daemon process via your service manager.

**Merge semantics**: every dispatched sweep auto-merges each PR after Judge approval — this is unconditional, not a separate mode. It does NOT skip code review — the Judge phase always runs inside the dispatched sweep.

**Full reference**: see `.loom/docs/daemon-reference.md` for the wire protocol, IPC request/response variants, registry internals, and reaper semantics.

---

### Sub-topic: sweep

**Sweep - Single-Issue Orchestration**

The sweep skill (`/loom:sweep <issue>`) orchestrates one issue through its complete lifecycle.

**Usage:**
```text
/loom:sweep 123                    # Run the full lifecycle for issue 123
/loom:sweep --prs 456 789          # Mode C — PR-set back half
/loom:sweep 123 --no-daemon        # Force in-session subagent dispatch
                                    # (skip Stage -1 daemon delegation)
```

**Lifecycle phases:**
```
1. Curator phase   - Enhance issue with implementation guidance
2. Builder phase   - Create worktree, implement, test, create PR
3. Judge phase     - Review PR, approve or request changes
4. Doctor phase    - Fix any requested changes (if needed)
5. Merge phase     - Auto-merge the approved PR
```

**Stage -1: Backend detection** (Phase D of #3449):

Before running phase 1, the sweep skill probes:
1. Is `loom-daemon` reachable? (Ping over IPC, 500ms timeout)
2. Does a multi-account token pool exist? (`.loom/tokens/` has ≥ 2 `ACCOUNT_KEY_*` entries)

**Strict AND** — if either probe fails, fall through to in-process subagent dispatch (the existing Mode A/B/C lifecycle, no behaviour change for solo-token operators). If both succeed AND the mode is not C AND `--no-daemon` is not set, the skill calls `mcp__loom__dispatch_sweep` and exits.

Mode C (`--prs`) always uses subagent dispatch; the daemon does not handle PR-set dispatch in v0.10.0.

The skill tracks progress via checkpoints in `.loom/sweep-checkpoint/issue-<N>.json` for crash recovery.

---

### Sub-topic: worktrees

**Git Worktree Workflow**

Loom uses git worktrees to isolate work per issue.

**Creating a worktree:**
```bash
./.loom/scripts/worktree.sh 42       # Creates .loom/worktrees/issue-42
cd .loom/worktrees/issue-42           # Branch: feature/issue-42
```

**Worktree locations:**
- `.loom/worktrees/issue-N` - Per-issue work (Builder creates these)

**Rules:**
- Always use `./.loom/scripts/worktree.sh` (never `git worktree` directly)
- Never delete worktrees manually - use `loom-clean`
- Worktrees auto-clean when PRs are merged

**Cleanup:**
```bash
loom-clean              # Interactive cleanup of stale worktrees
loom-clean --force      # Non-interactive cleanup
loom-clean --deep       # Also remove build artifacts
```

---

### Sub-topic: labels

**Label Reference**

**Workflow labels (issue lifecycle):**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:curating` | Curator is actively enhancing | Curator |
| `loom:curated` | Issue enhanced, awaiting approval | Curator |
| `loom:issue` | Approved and ready for work | Champion/Human |
| `loom:building` | Builder is implementing | Builder |
| `loom:blocked` | Work is blocked | Builder |
| `loom:operator-only` | Requires human action; sweep skip | Human |
| `loom:urgent` | Critical priority | Guide/Human |

**Workflow labels (PR lifecycle):**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:review-requested` | PR ready for review | Builder |
| `loom:changes-requested` | PR needs fixes | Judge |
| `loom:pr` | PR approved, ready to merge | Judge |
| `loom:auto-merge-ok` | Override a Champion merge-risk hold | Judge/Human |

**Proposal labels:**

| Label | Meaning | Set by |
|-------|---------|--------|
| `loom:architect` | Architecture proposal | Architect |
| `loom:hermit` | Simplification proposal | Hermit |
| `loom:auditor` | Bug found by Auditor | Auditor |

---

### Sub-topic: troubleshoot

**Troubleshooting**

**Issue stuck in `loom:building`:**
```bash
loom-recover-orphans --recover
```

**Orphaned sweeps after daemon crash:**
```bash
loom-recover-orphans --recover
```

**Labels out of sync:**
```bash
gh label sync --file .github/labels.yml
```

**Stale worktrees/branches:**
```bash
loom-clean --force
```

**Daemon unreachable:**
First rule out an MCP-only problem: if `mcp__loom__list_sweeps` errors or hangs
but no `mcp__loom__*` tools are registered at all (or you suspect the historical
#4043 MCP-bridge wedge), try `loom-daemon status` — it talks to the daemon over
its Unix socket directly, with no MCP bridge in the path. Only conclude the
daemon itself is down if `loom-daemon status` *also* fails; then verify the
binary is running outside Claude Code (via your service manager) and restart it.

**Cancel a stuck sweep:**
```
mcp__loom__cancel_sweep --sweep_id <id>
```
No CLI equivalent — if MCP is unavailable, get the PID from `loom-daemon status`
and `kill -TERM <pid>` directly (the checkpoint survives, so a later dispatch
resumes from the last completed phase).

**Inspect a sweep's log:**
```
mcp__loom__tail_sweep_log --issue <N> --lines 200
```

**Subscribe to events for live debugging:**
```
mcp__loom__subscribe_to_events --topic "sweep.issue.<N>.*"
mcp__loom__tail_event_bus
```

**Merge PRs from worktrees (never use `gh pr merge`):**
```bash
./.loom/scripts/merge-pr.sh <PR_NUMBER>
```

**Reference documentation:**
- Daemon details: `.loom/docs/daemon-reference.md`
- Sweep lifecycle: `defaults/.claude/commands/loom/sweep.md`
- Full troubleshooting: `.loom/docs/troubleshooting.md`
