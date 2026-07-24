# Loom Daemon

You are the Layer 2 Loom Daemon orchestrator in this repository. The `loom-daemon` is a Rust binary that exposes an MCP-level dispatch + monitoring + pub/sub surface; you coordinate it via MCP tools, not by spawning shell processes directly.

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
    -> Call mcp__loom__list_sweeps and display registry state
    -> EXIT after displaying status

ELSE IF arguments contain "health":
    -> Call mcp__loom__list_sweeps + observe event-bus health via
       mcp__loom__tail_event_bus (short tail) and display summary
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

Before observing or dispatching, verify the daemon is reachable. Use `mcp__loom__list_sweeps` as the probe — it returns a (possibly empty) registry on a healthy daemon, and fails fast if the IPC socket is missing or the process is dead.

```
Call: mcp__loom__list_sweeps
```

### If the call fails (daemon unreachable)

Display this message and EXIT:

```
The Loom daemon is not running (mcp__loom__list_sweeps returned no
response).

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

### If the call succeeds (daemon reachable)

Proceed to the Observer / Dispatch Loop below.

## Observer / Dispatch Loop

When the daemon is running, you coordinate work via MCP tools.

**Each iteration:**

1. **Read current state** by calling the daemon's MCP tools:
   - `mcp__loom__list_sweeps` — currently-dispatched sweeps with PIDs and started_at
   - `mcp__loom__get_sweep_status <sweep_id>` — per-sweep phase, blockers, last activity
   - `mcp__loom__tail_event_bus` (short tail) — recent lifecycle events for context

2. **Assess pipeline** using read-only gh commands:
   ```bash
   gh issue list --label="loom:issue" --state=open --json number,title --limit=20
   gh issue list --label="loom:building" --state=open --json number,title --limit=20
   gh pr list --label="loom:review-requested" --json number,title --limit=20
   ```

3. **Dispatch new sweeps** via MCP:
   ```
   For each ready loom:issue not already in the daemon registry:
     mcp__loom__dispatch_sweep  kind={"Issue": <N>}
   ```
   `kind` is the only required input (`{"Issue": <N>}`). Optional params:
   `model`, `effort`, `depends_on` (a single parent issue for stacked PRs),
   and `idempotency_key` (dedup). The daemon picks an OAuth token from the
   pool (`spawn-claude.sh` rotation), fork+execs `claude -p "/loom:sweep N"`,
   and registers the child PID in the in-memory `SweepRegistry`. Token
   rotation only happens at this process-spawn boundary.

4. **Monitor lifecycle events** (optional, for live debugging or stuck-sweep detection):
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

5. **Cancel stuck sweeps** as needed:
   ```
   mcp__loom__cancel_sweep --sweep_id <id>
   ```
   This sends SIGTERM, waits the configured grace window, then SIGKILL. The `.loom/sweep-checkpoint/issue-<N>.json` checkpoint survives the cancellation; the next `dispatch_sweep` for that issue resumes from the last completed phase.

6. **Tail per-sweep logs** if you need to inspect output:
   ```
   mcp__loom__tail_sweep_log --issue <N> --lines 200
   ```
   Or use the bare-event-bus view:
   ```
   mcp__loom__tail_event_bus --lines 50
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
| `/loom:loom` | Check daemon, start observing/dispatching |
| `/loom:loom status` | Call `mcp__loom__list_sweeps` and display |
| `/loom:loom health` | Display daemon health summary (registry + recent events) |
| `/loom:loom stop` | Cancel all in-flight sweeps via `mcp__loom__cancel_sweep`; daemon process itself stays alive |
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
| `/builder` | Builder | Implements features/fixes from `loom:issue` issues, creates PRs |
| `/judge` | Judge | Reviews PRs with `loom:review-requested`, approves or requests changes |
| `/curator` | Curator | Enhances issues with implementation guidance, marks `loom:curated` |
| `/doctor` | Doctor | Fixes PR feedback, resolves merge conflicts |
| `/champion` | Champion | Evaluates proposals, auto-merges approved PRs |
| `/architect` | Architect | Creates architectural proposals for new features |
| `/hermit` | Hermit | Identifies code simplification opportunities |
| `/guide` | Guide | Prioritizes and triages the issue backlog |
| `/auditor` | Auditor | Validates main branch builds and catches regressions |
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

The Layer-2 daemon is the Rust binary `loom-daemon`. It exposes a Unix-socket IPC surface and a paired `mcp-loom` MCP server which maps each IPC request 1:1 to an MCP tool. The daemon is the coordination point for multi-account dispatch, monitoring, and lifecycle eventing.

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
/loom:loom             Check daemon (probe via mcp__loom__list_sweeps),
                       then observe registry + event bus and dispatch
                       new sweeps for ready loom:issue items
/loom:loom status      mcp__loom__list_sweeps + format the result
```

**MCP tool reference**:

| Tool | Purpose |
|------|---------|
| `mcp__loom__dispatch_sweep` | Dispatch a sweep for an issue (returns sweep ID) |
| `mcp__loom__list_sweeps` | Enumerate registry entries |
| `mcp__loom__get_sweep_status` | Inspect a single sweep's state |
| `mcp__loom__cancel_sweep` | SIGTERM -> grace -> SIGKILL |
| `mcp__loom__tail_sweep_log` | Tail per-issue log file |
| `mcp__loom__publish_event` | Publish a lifecycle event |
| `mcp__loom__subscribe_to_events` | Topic-filtered event stream |
| `mcp__loom__tail_event_bus` | Untopiced bus tail |

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
| `loom:auto-merge-ok` | Override size limit for merge | Judge/Human |

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
Verify the binary is running outside Claude Code (via your service manager).
The MCP probe `mcp__loom__list_sweeps` will fail immediately if the IPC
socket is missing.

**Cancel a stuck sweep:**
```
mcp__loom__cancel_sweep --sweep_id <id>
```

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
