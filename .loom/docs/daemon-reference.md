# Loom Daemon Reference

> **Status: ACTIVE (v0.10.0).** This page describes the Rust `loom-daemon`
> binary and its MCP-facing surface — the dispatch + pub/sub + monitoring
> tools delivered by epic #3449 (Phases A through C). The legacy Python
> `loom-daemon` brain (`loom_tools/daemon_v2/`) and the `/shepherd`
> orchestrator were deleted in the v0.10.0 deprecation epic (#3372). The
> shell-level `./.loom/scripts/daemon.sh` tmux session launcher (when
> rebuilt under epic #3449's later phases) wraps this same daemon binary.

## What the daemon is

`loom-daemon` is a Rust process that exposes a Unix-socket IPC surface
(framed JSON, line-delimited) and a paired `mcp-loom` MCP server which
maps each IPC request 1:1 to an MCP tool. The daemon is **the
coordination point** for:

- **Dispatching** `/loom:sweep` children with multi-account OAuth token
  rotation (via `defaults/scripts/spawn-claude.sh`).
- **Tracking** running sweeps in an in-memory registry (no on-disk state
  file — the forge is the source of truth for queue state).
- **Publishing** sweep-lifecycle events on an in-memory pub/sub bus, and
  **subscribing** external monitors to topic-filtered streams.
- **Cancelling** in-flight sweeps with SIGTERM → grace → SIGKILL.
- **Reaping** dead PIDs (every 30s) to maintain registry liveness and
  emit `sweep.issue.*.exited` / `sweep.issue.*.crashed` events.

**By default it is not a work generator.** With no autonomous config it
does not poll the forge for ready issues, it does not maintain a
`shepherd-N` pool, and it does not run support roles on cron — those
responsibilities live in `mcp__loom__dispatch_sweep` (operator-driven
enqueue) and the GitHub Actions cron workflows
(`.github/workflows/loom-*.yml`). Two **opt-in, default-off** surfaces
(epics #3809 and #3842) let the daemon generate and dispatch its own work
when explicitly enabled: the [autonomous work
finder](#autonomous-work-finder-3810) polls open `loom:issue` items and
auto-dispatches sweeps, and the [epic supervisor](#epic-supervisor-3842)
drives `loom:epic` fork-joins. See [Operability](#operability--config-startstop-e2e-phase-d-3813)
for enabling and tuning them.

## The `LOOM_ROLE` env-var contract (#6507)

`LOOM_ROLE` is a **public, stable session-identity contract**: repo-level
hooks/scripts (SessionStart hooks, guard hooks, any tooling that needs to
tell an operator's interactive session apart from a session Loom itself
spawned) **may branch on `LOOM_ROLE` being set** to detect "this is a
Loom-spawned agent session." Two shipped repo-level consumers already do
exactly this — `.loom/hooks/guard-destructive-generic.sh` (the
`dist/`-write-confinement role allowlist, #6021) and
`.loom/hooks/methodology-inject.sh` (role-context injection keyed off
`LOOM_ROLE` matching a `roles/<role>.md` filename) — see
[`guard-hooks.md`](guard-hooks.md) for the former.

**Semantics:**

- **Present** ⇒ this session was spawned by Loom (a dispatched sweep child,
  a role-runner tick, or a tmux/MOM agent launch), and its value names the
  role (or, for a whole `/loom:sweep` launch, the sentinel role
  `sweep-lifecycle`).
- **Absent** ⇒ not spawned by Loom — e.g. an interactive operator session
  (see the one residual gap below).

**What sets it** — every production code path that spawns an agent session
sets `LOOM_ROLE`, unconditionally once past its own admission/validation
step (traced against `origin/main` @ `7d169a06`, 2026-08-18; re-verify
against current `main` before relying on a specific line number, since a
new spawn call site could be added later):

1. `loom-daemon/src/sweep_registry/dispatch.rs` (~line 2089, inside
   `spawn_child`) — sets `LOOM_ROLE` to the already-admitted
   `admission.role` (always `"sweep-lifecycle"` for a full `/loom:sweep`
   launch) alongside `LOOM_RUNTIME`. Gated by `if let Some(admission) =
   runtime_admission`, but `runtime_admission` is only ever `None` when
   `self.config.skip_label_flip` is true — a test-only fixture flag
   (hermetic unit tests skip installing runtime manifests); every real
   dispatch either admits successfully or returns `Err` before the spawn
   is ever reached (Issue #4768).
2. `loom-daemon/src/role_runner.rs` (~line 1212, inside
   `run_role_with_timeout`) — same shape, for a periodic support-role tick
   (Champion/Curator/Judge/Doctor/Auditor/Guide/Hermit/Architect). Gated by
   `if let Some(admission) = admission`, where `admission` is only ever
   `None` when `self.spawn_bin.is_some()` — again a test-only condition (a
   fake spawn script substituted in tests); the production path
   (`spawn_bin` unset) always resolves a real admission or bails out before
   spawning (Issue #4768).
3. `loom-daemon/src/agent_session/spawn.rs` (~line 647, inside the
   tmux/MOM `run()` path used by `agent-spawn.sh` / interactive terminal
   launch) — sets `LOOM_ROLE` to `opts.role` **unconditionally**, no `if
   let Some` gate at all. `opts.role.is_empty()` is already rejected
   earlier in the same function (returns exit `1` before any tmux session
   is created), so by the time this line runs `opts.role` is guaranteed
   non-empty.

**Known residual gap**: an operator manually typing `/loom:<role>` (e.g.
`/loom:doctor`) inside an *already-running* interactive session produces a
role agent with **no `LOOM_ROLE` in its environment** — the session itself
was never spawned by any of the three paths above, so there is nothing for
a spawner to set. This case is structurally outside what an env-var
contract can cover (there is no spawn event to hook), and is handled
instead on the consumer side via a prompt-conditional directive. Do not
treat an unset `LOOM_ROLE` as proof no Loom role is active in the current
prompt — only as proof the *session* was not Loom-spawned.

A regression test pins this invariant against silent regression on the
`dispatch.rs` and `role_runner.rs` admission-success paths (the two call
sites gated by an `if let Some`) — see
`dispatch_sets_loom_role_from_admitted_role` (Issue #4768, pre-existing) and
`invoke_sets_loom_role_env_on_admitted_spawn` (Issue #6507) in
`loom-daemon/src/sweep_registry/dispatch.rs` and
`loom-daemon/src/role_runner.rs` respectively, plus
`test_run_sets_loom_role_session_env` in
`loom-daemon/src/agent_session/spawn.rs` for the unconditional tmux path.

`LOOM_RUNTIME`'s own contract is documented separately in
[`runtime-adapters.md`](runtime-adapters.md) — not covered here.

## Architecture (Phases A-C)

```
┌────────────────────────────────────────────────────────────────┐
│                      MCP clients (Claude Code)                 │
│  - dispatch_sweep, list_sweeps                          (A)    │
│  - publish_event, subscribe_to_events                   (B)    │
│  - get_sweep_status, tail_sweep_log, cancel_sweep       (C)    │
│  - tail_event_bus                                       (C)    │
└────────────────────────────────────────────────────────────────┘
                              │ stdio JSON-RPC
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                    mcp-loom (TypeScript)                       │
│  - Validates args, normalizes payloads, formats output         │
│  - One MCP tool per IPC Request variant                        │
└────────────────────────────────────────────────────────────────┘
                              │ Unix socket, line-delimited JSON
                              ▼
┌────────────────────────────────────────────────────────────────┐
│                    loom-daemon (Rust)                          │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │ SweepRegistry    │  │ EventBus         │  │ ReaperTask   │  │
│  │ (BTreeMap)       │  │ (broadcast chan) │  │ (30s tick)   │  │
│  └──────────────────┘  └──────────────────┘  └──────────────┘  │
│                              │                                  │
│                              ▼                                  │
│                    fork+exec /loom:sweep N                      │
│                    via spawn-claude.sh                          │
└────────────────────────────────────────────────────────────────┘
                              │ detached child
                              ▼
                       /loom:sweep <issue>
                       (Claude Code session)
```

## IPC surface (Request/Response variants)

The wire protocol is line-delimited JSON. Each `Request` is one line; the
daemon responds with one line per request — except `SubscribeEvents`,
which holds the connection open and streams one `EventStream` frame per
event. Connection framing matches the existing terminal-management IPC
surface; no new transport is introduced.

Source of truth: [`loom-daemon/src/types.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/types.rs)
(upstream Loom repo — not shipped to consumer installs).

| Request | MCP tool | Response | Phase |
|---------|----------|----------|-------|
| `DispatchSweep`     | `dispatch_sweep`       | `SweepDispatched`   | A (#3452) |
| `ListSweeps`        | `list_sweeps`          | `SweepList`         | A (#3452) |
| `PublishEvent`      | `publish_event`        | `EventPublished`    | B (#3453) |
| `SubscribeEvents`   | `subscribe_to_events`, `tail_event_bus` | `EventStream` (stream) | B (#3453) |
| `GetSweepStatus`    | `get_sweep_status`     | `SweepStatus`       | C (#3455) |
| `TailSweepLog`      | `tail_sweep_log`       | `SweepLogTail`      | C (#3455) |
| `CancelSweep`       | `cancel_sweep`         | `SweepCancelled`    | C (#3455) |

## Event taxonomy (frozen for v0.10.0)

The bus accepts arbitrary topic strings, but the documented taxonomy is
the contract subscribers should rely on. **New topics require a follow-up
issue** — the v0.10.0 set is intentionally frozen.

| Topic | Publisher | Payload |
|-------|-----------|---------|
| `sweep.issue.{N}.phase`   | Sweep child via `publish_event` | `{phase, pr_number?, repo?}` |
| `sweep.issue.{N}.blocker` | Sweep child                     | `{reason, label_added, repo?}` |
| `sweep.issue.{N}.exited`  | Daemon reaper (or `cancel_sweep`) | `{exit_code, duration_sec, no_progress, repo?}` |
| `sweep.issue.{N}.crashed` | Daemon reaper                   | `{checkpoint_phase, repo?}` |
| `sweep.issue.{N}.resume_dispatched` | Daemon reaper (#4256) | `{pr, checkpoint_phase?, dispatched, repo?}` |
| `sweep.global.dispatch`   | Daemon                          | `{sweep_id, kind}` |
| `sweep.global.completed`  | Daemon                          | `{sweep_id, outcome}` |
| `epic.issue.{N}.decompose` | Epic supervisor (#3842)        | `{epic, action, state}` |
| `epic.issue.{N}.expand`    | Epic supervisor (#3842)        | `{epic, action, state}` |
| `epic.issue.{N}.join`      | Epic supervisor (#3842)        | `{epic, action, state}` |
| `epic.issue.{N}.close`     | Epic supervisor (#3842)        | `{epic, action, state}` |
| `daemon.capacity.advisory` | Work finder (#3902)            | `{pressured, queued, healthy_accounts, exhausted_accounts, total_accounts, estimated_drain_minutes?, message}` |
| `daemon.drain.started`     | Drain supervisor (#4090)       | `{in_flight, timeout_secs, force_after_timeout, deadline}` |
| `daemon.drain.completed`   | Drain supervisor (#4090)       | `{in_flight}` (always `0`) |
| `daemon.drain.aborted`     | Daemon IPC (#4090)             | `{was_draining}` |
| `daemon.drain.timeout`     | Drain supervisor (#4090)       | `{in_flight, forced, cancelled?, then_exit?, roll_pending?, attempts?, elapsed_secs?}` |
| `daemon.drain.roll_pending` | Drain supervisor (#6007)      | `{in_flight, attempt, window_secs, budget_secs}` |

The four `epic.issue.{N}.*` topics were authorized by **#3873** (epic #3842
Phase 4) and are documented in full under [Epic supervisor](#epic-supervisor-3842)
below. The `daemon.capacity.advisory` topic was authorized by **#3902** (epic
#3809): the autonomous work finder publishes it on a token-capacity **pressure
state change** (entered/left the token-bound state), never every tick, so the
operator gets one add-capacity advisory on the way in and one recovery on the way
out. See [Token-capacity backpressure](#token-capacity-backpressure-3902) below.
Four of the `daemon.drain.*` topics were authorized by **#4090** for the scheduled
drain-and-restart primitive — `started` when a drain is accepted, `completed`
when the last in-flight sweep finishes (right before the supervised relaunch),
`aborted` when an operator cancels a drain, and `timeout` when the deadline is
reached *terminally* (`forced` distinguishes a refusal from a force-cancel
restart). **#6007** adds a fifth, `roll_pending`: a relaunch drain whose deadline
passed with work still in flight now **retains** the roll (dispatch stays paused,
the restart re-arms itself at quiescence) and publishes `roll_pending` per re-arm;
`timeout` then fires only if the whole paused-dispatch budget is spent and the roll
is abandoned. See
[Supervised restart primitive](#supervised-restart-primitive-4054) below.
They ride the same in-memory bus as the sweep topics and are tailable via
`subscribe_to_events` / `tail_event_bus`.

The four `sweep.issue.{N}.*` payloads gained an additive **`repo`** field in
**#3929** (`(repo, issue)`-aware sweep visibility, phase c of #3926). The bus is
shared across every managed repo, and the topic string is issue-scoped only —
two managed repos can each dispatch a sweep for issue #42 onto the *identical*
`sweep.issue.42.phase` topic. `repo` carries the owning registry's
`workspace_root`, so a multi-repo-aware subscriber can disambiguate them after
matching the topic. **The topic strings are unchanged** — `repo` lives in the
payload only, so existing single-repo subscribers that filter on `sweep.issue`
or `sweep.issue.{N}` route byte-for-byte identically and simply ignore the new
field. The daemon stamps `repo` centrally when it emits each event; a sweep
child that already knows its repo (via `publish_event`) may supply it and will
not be overwritten. `sweep.global.*` events are unchanged — they already carry a
unique `sweep_id`.

`sweep.issue.{N}.exited` gained an additive **`no_progress`** boolean field in
**#4366**. It classifies a checkpoint-less clean exit (`exit_code == Some(0)`)
as a genuine no-lifecycle-progress death (`true`) vs. an ordinary/benign exit
(`false`) — `true` requires ALL of: clean exit, no open PR linked to the issue,
and the issue still open. The reaper counts a `true` verdict toward the
insta-crash quarantine tally (via the same `record_terminal_outcome` path the
#3939 insta-crash arm uses) instead of resetting it, closing the gap where a
headless sweep that ends its turn parked on a monitored background task (e.g.
"cache download is running in the background... I'll pick this back up") exits
0 with zero checkpoint/PR progress and previously reset the tally forever,
letting the dispatch queue churn on it indefinitely. `#[serde(default)]` keeps
pre-#4366 wire data compatible (deserializes to `false`, matching prior
behavior). Gated on `!skip_label_flip` like the reaper's other real-forge
probes — under `skip_label_flip` the field is always `false` and no `gh` call
is made.

In addition, the bus internally emits:

- `sweep.system.topic_lag` — synthetic event when a subscription falls
  behind the publisher past the bus capacity. Mirrors tokio's `Lagged`
  semantics; carries `{skipped: usize}`.

Topic matching is **segment-aligned prefix** (`sweep.issue` matches
`sweep.issue.123.phase` but not `sweep.issuetype.foo`). See
[`event_bus::topic_matches`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/event_bus.rs)
(upstream Loom repo — not shipped to consumer installs) for the
authoritative routing rule.

## MCP tool reference

All tools live in `mcp-loom/src/tools/sweeps.ts`. Each tool name maps
1:1 to an IPC `Request` variant.

### `dispatch_sweep`

Spawn a `/loom:sweep` child via the daemon's registry. The daemon shells
out to `defaults/scripts/spawn-claude.sh` for token rotation and detaches
the child. Returns the `sweep_id`, child PID, token-account name, and
per-sweep log path.

Inputs:
- `kind` (required) — `{"Issue": <N>}` (spawns `/loom:sweep <N> --claim-owned
  <N>`, the full Curator/Builder/Judge/Doctor/Merge lifecycle for one issue)
  or `{"PrSet": [<n1>, <n2>, ...]}` (Mode C, issue #5342 — spawns
  `/loom:sweep --prs <n1> <n2> ...`, driving Judge/Doctor -> Judge/Merge for
  an existing PR set without re-running Curator/Builder). `PrSet` claims no
  issue and no `loom:building` label; each PR is claimed with its own
  `.loom/locks/pr-<N>/` lock, distinct from `Issue` dispatch's
  `.loom/locks/issue-<N>/`.
- `idempotency_key` (optional) — dedup key. Running sweeps with the same
  key return the existing `sweep_id` without spawning a new child.
- `model` (optional, issue #3477 Phase 1) — Claude model for the spawned
  child, as an alias (`sonnet`, `opus`, `haiku`) or a pinned ID
  (`claude-sonnet-4-6`). Forwarded as `--model <value>` on the
  `spawn-claude.sh` argv. When omitted (or empty), NO `--model` flag is
  emitted and the child inherits the session/CLI default. The field is
  `#[serde(default)]` on the wire, so pre-#3477 clients remain compatible.
- `depends_on` (optional, issue #3729 stacked-PR v1) — a **single** parent
  issue number this sweep is stacked on. Forwarded to the child as
  `--depends-on <N>` (mirroring the `--model`/`--effort` append-only,
  empty-means-unset contract), instructing `/loom:sweep` to branch the child
  worktree/PR off `feature/issue-<N>` instead of the default branch. When
  omitted, NO `--depends-on` flag is emitted (byte-for-byte unchanged). A
  single optional parent (not a list) makes diamonds / multi-parent stacks
  structurally unrepresentable — see "Stacked-PR dependency (v1)" below. The
  field is `#[serde(default)]` on the wire, so pre-#3729 clients remain
  compatible.
- `workspace_root` (optional, issue #3929) — target managed-workspace root.
  When set to a registered repo root, the daemon resolves that repo's sweep
  registry via the `WorkspacePool` and dispatches into its working tree — the
  way to dispatch into a managed repo other than the default when two repos
  share issue numbers. `#[serde(default)]` on the wire.

  **When omitted (issue #4299):** the daemon no longer blindly targets its own
  seeded default (cwd at startup / `LOOM_WORKSPACE`) — it consults the
  on-disk `~/.loom/workspaces.json` registry (`WorkspaceRegistry::resolve_dispatch_root`,
  `workspace_registry.rs`) in this order:
  1. **Registry empty** -> the seeded default (byte-for-byte pre-#4299 / pre-registry
     behavior).
  2. **Seeded default is itself registered** -> the seeded default. This is the
     back-compat floor: every existing multi-workspace host runs the daemon
     from a registered repo, so a bare dispatch with no `workspace_root` keeps
     working with no new flags.
  3. **Seeded default is NOT registered, exactly one workspace is registered**
     -> that workspace. This is the case a single-repo worker host (daemon cwd
     = the machine checkout, one product repo registered) needs: the sole
     registration is the only sane target.
  4. **Seeded default is NOT registered, multiple workspaces are registered**
     -> a structured `CONFIG_WORKSPACE_AMBIGUOUS` error naming every
     registered root. Issue numbers are per-repo, so guessing which repo
     "owns" issue N by probing the forge is ill-defined — an explicit
     `workspace_root`/`--workspace` is required instead. Never a silent cwd
     fallback.

  This resolution applies to the **dispatch path only**. `list_sweeps`,
  `get_sweep_status`, and quarantine requests keep their unconditional
  default-registry fallback for an absent `workspace_root` — read-path default
  behavior is unchanged (a deliberate scope limit; see the #4299 issue for the
  follow-up if that also needs to change).

#### `loom-daemon dispatch <issue>` — operator CLI (Issue #3952)

`loom-daemon dispatch <issue>` is the **non-MCP** operator entry point onto the
same `DispatchSweep` IPC request the `dispatch_sweep` MCP tool uses. It is a thin
client: connect to the daemon socket, send one `DispatchSweep` frame, print the
returned `sweep_id` + per-sweep log path, exit `0`. Because it flows through the
registry, the `loom:issue → loom:building` claim flip, in-flight tracking, the
reaper, and event publishing all come for free — exactly like the MCP path.

```bash
loom-daemon dispatch 3952                          # dispatch into the default workspace
loom-daemon dispatch 3952 --workspace /path/to/repo # target a registered managed repo (#3929)
loom-daemon dispatch 3952 --model sonnet --effort high
loom-daemon dispatch 3952 --depends-on 3945        # stacked-PR child (#3729)
```

| Flag | Maps to `DispatchSweep` field | Notes |
|------|-------------------------------|-------|
| `<issue>` (positional) | `kind = {"Issue": N}` | required |
| `--workspace <PATH>` | `workspace_root` | target a registered repo other than the default (#3929) |
| `--model <M>` | `model` | omit to let the daemon resolve `autonomous.model` / the shipped default (#3944) |
| `--effort <E>` | `effort` | reasoning-effort override (#3716) |
| `--depends-on <P>` | `depends_on` | single parent issue; child branches off `feature/issue-<P>` (#3729) |

**`--workspace` client-side cwd default (issue #4299).** When `--workspace` is
omitted, the CLI itself (not the daemon — it cannot see the client's cwd)
checks whether its own working directory falls under a registered workspace
root and, if so, populates `workspace_root` with that root before sending the
request (`resolve_cli_dispatch_workspace` in `main.rs`). This is what makes
`cd ~/GitHub/anvil && loom-daemon dispatch 758` target anvil even when the
daemon's own seeded default points elsewhere. A cwd outside every registered
root leaves `--workspace` unset, and the daemon-side resolution above applies.

**Bounded ack timeout (never hangs).** The CLI waits at most **30s** for the
daemon to ack the dispatch, then exits **nonzero** with a clear
`Daemon did not ack the dispatch within 30s (...) — is loom-daemon running?`
message rather than blocking. The 30s default mirrors the `mcp__loom__dispatch_sweep`
tool's own `DISPATCH_TIMEOUT_MS` for the identical IPC call, and exists because
`SweepRegistry::dispatch()` does real synchronous work *before* it acks — a
blocking `gh issue edit` label flip, up to a 2s dispatch stagger, and up to a 5s
token-name capture window — so a legitimate, successful dispatch can take several
seconds to ack. A tighter bound (the original 5s) would false-report those real
successes as `did not ack`. Operators on a slow forge or a heavily-loaded daemon
can *raise* the bound with `LOOM_DAEMON_IPC_TIMEOUT_MS=<ms>` (the same env var
`mcp-loom` honors); it only ever raises above the 30s floor, never lowers it (a
lower value would reintroduce the false negative). The timeout is always a
bounded, finite value: the MCP `dispatch_sweep` path once wedged for **1800s**
(#3945), and this command must never reproduce that hang.

**Replaces the hand-rolled pattern.** Before #3952 the only non-MCP alternative
was to reproduce the daemon's dispatch by hand — flip the label, export
`LOOM_SWEEP_CLAIM_OWNED=<N>` plus `LOOM_MODEL` and workaround envs, and invoke
`spawn-claude.sh -p "/loom:sweep N"` directly:

```bash
# DEPRECATED — do NOT do this. Bypasses the registry (no in-flight tracking,
# no reaper, no status visibility), and — even carrying both the correct env
# var AND the prompt-embedded `--claim-owned N` marker `SweepRegistry::dispatch`
# itself emits (#4111; embedded in the -p prompt text, NOT a sibling claude arg,
# which the real claude CLI would reject — #4120) — is fragile to reproduce by
# hand; a mismatched/missing claim marker on either channel makes the child skip
# its own issue.
gh issue edit 3952 --remove-label loom:issue --add-label loom:building
LOOM_SWEEP_CLAIM_OWNED=3952 LOOM_MODEL=sonnet \
  ./.loom/scripts/spawn-claude.sh -p "/loom:sweep 3952 --claim-owned 3952"
```

Use `loom-daemon dispatch 3952` instead — it performs the claim flip, registry
tracking, and event publishing for you, with the bounded timeout as a safety net.

#### `loom-daemon cancel` — operator CLI (Issue #4980)

`loom-daemon cancel` is the `dispatch` sibling on the **stop** side: the non-MCP
operator entry point onto the same `CancelSweep` IPC request the `cancel_sweep`
MCP tool uses. It exists because the MCP tool only runs inside the operator's own
Claude session on their own machine — over ssh to a fleet worker there was
previously no sanctioned lever at all.

```bash
loom-daemon cancel sweep-issue-4980-1762000000   # by sweep id (from `loom-daemon status`)
loom-daemon cancel --issue 4980                  # by issue — resolves the live sweep for you
loom-daemon cancel --issue 4980 --grace 5        # tighter SIGTERM→SIGKILL window (default 30s)
loom-daemon cancel --issue 758 --workspace ~/GitHub/anvil
```

| Flag | Maps to `CancelSweep` field | Notes |
|------|-----------------------------|-------|
| `<sweep-id>` (positional) | `sweep_id` | required unless `--issue` is given |
| `--issue <N>` | `sweep_id` (resolved client-side) | one extra `ListSweeps` round-trip; only **live** (`Running`/`Pending`) entries are candidates, and more than one is an error rather than a guess |
| `--grace <SECS>` | `grace_secs` | seconds between SIGTERM and SIGKILL; default 30, matching the MCP tool |
| `--workspace <PATH>` | `workspace_root` | same cwd-default resolution `dispatch` applies (#4299) |

**Never hand-`kill` a sweep instead.** The registry tracks the
`claude-wrapper.sh` pid; SIGKILLing it leaves the underlying `claude` agent
alive. On 2026-08-03 that surviving agent noticed its subprocesses had died and
**relaunched** them (~35 CPU-hours of simulation across two rounds), against an
issue whose claim the crash path had already returned to `loom:issue` — a zombie
agent the registry reported as `in_flight: 0`. `loom-daemon cancel` signals the
whole process **group**, so wrapper, agent, and every descendant die together,
and it releases the claim lock, restores the label, and emits the lifecycle
events on the way out.

**One implementation, two callers.** The CLI is a thin client: the termination
itself runs daemon-side in `cancel_sweep_nonblocking`, so `loom-daemon cancel`
and `mcp__loom__cancel_sweep` cannot drift apart. The client-side ack budget is
`grace_secs + 15s` (the daemon acks only after the whole cancel completes), and
on expiry the CLI exits nonzero with the same "is loom-daemon running?"
diagnostic `dispatch` uses rather than hanging.

#### Process-group termination and the persisted `pgid` (#3800, #4980)

Sweep children are spawned as their **own process-group leader**
(`process_group(0)` → `setpgid(0, 0)`), so `kill(-pgid, sig)` reaches the child
*and* every descendant it forked — Bash-tool commands, MCP servers, git clones,
simulations, watcher loops.

#3800 delivered the group spawn and the group-kill primitive, but gated the group
path on the daemon still holding the spawn-time `Child` handle. Two ordinary
situations have no such handle and silently degraded to a single-PID kill that
orphaned the whole subtree: an entry rebuilt by `reconstruct()` after a daemon
restart, and **every** `loom-daemon cancel` invocation (a fresh process that
never held one). #4980 closes that by persisting the group:

- **At spawn** the child's pgid is verified against the OS (`getpgid(child) ==
  child`) and written into `.loom/locks/issue-<N>/owner.json` as `pgid`, and onto
  the registry entry as `SweepInfo.pgid`.
- **On reconstruct** the persisted value is restored — but re-verified against
  the live owner first; a value the OS contradicts (a PID recycled across the
  restart) is discarded rather than used to signal a stranger's group.
- **On signal** the group path is unconditional. A missing `pgid` (a pre-#4980
  `owner.json`, a checkpoint-only entry) degrades to single-PID delivery **with a
  log line**, never silently; a `pgid` matching the daemon's own group is refused
  outright.
- **On the crash path** — a dead leader whose group still has members, the zombie-
  agent shape above — the reaper (and `reconstruct()`'s stale-lock branch) sends
  the group a SIGTERM and escalates to SIGKILL on a later tick if it survives.
  The persisted value is the *only* handle available here: the OS will not report
  a dead pid's process group.

`owner.json`'s `pgid` is `Option` + `#[serde(default)]`, so a lock written by a
pre-#4980 daemon still deserializes — failing to parse it would be read
everywhere as "no owner" and would drop a *live* sweep's lock.

### `list_sweeps` (Phase A)

Return all tracked sweeps, optionally filtered by lifecycle state.
Terminal entries are garbage-collected ~1h after the transition.

Inputs:
- `state_filter` (optional) — one of `Pending`, `Running`, `Exited`,
  `Crashed`.
- `workspace_root` (optional, issue #3929) — target managed-workspace root.
  Omit to list the default workspace's sweeps (unchanged). Set to a registered
  repo root to list the sweeps tracked by that repo's registry — the way to
  observe sweeps the daemon autonomously dispatched into a non-default managed
  repo. Each returned `SweepInfo` also carries a `repo` field naming its owner,
  so a response is self-describing even without filtering. Ignored when
  `all_workspaces` is `true`. `#[serde(default)]` on the wire.
- `all_workspaces` (optional, issue #6006) — fan out across every registered
  managed workspace (`WorkspaceRegistry::effective_roots`, the same enumeration
  `list_quarantines`'s `None` case and `loom-daemon status`'s `per_repo` use)
  and return the aggregated fleet-wide sweep set in one call, sorted by
  `(repo, started_at)`. No prior knowledge of individual repo roots is needed —
  an empty registry still yields exactly the default workspace, so a
  single-workspace daemon's fan-out equals `workspace_root: None`. `false` (or
  absent — `#[serde(default)]`) preserves the existing `workspace_root`-scoped
  (or default-workspace-only) behavior byte-for-byte; `workspace_root` is
  ignored when this is `true`.

The same optional `workspace_root` input (default = default workspace, unchanged)
is accepted by `get_sweep_status`, `tail_sweep_log`, and `cancel_sweep` — so a
sweep the daemon dispatched into a non-default managed repo can be inspected,
tailed, and cancelled by naming its repo root (#3929).

### `publish_event` (Phase B)

Publish a JSON event onto the in-memory bus. Operator override / test
escape hatch — production publishes happen via the sweep skill, not this
tool.

Inputs:
- `topic` (required) — should follow the frozen taxonomy.
- `payload` (required) — opaque JSON.

### `subscribe_to_events` (Phase C)

Open a long-lived subscription to the event bus, filtered by topic
prefix. Frames arrive as line-delimited JSON matching
`Response::EventStream { events: [Event] }`. The MCP layer caps each
subscription with a `duration` window so a single tool call returns
deterministically.

Inputs:
- `topics` (optional) — array of topic prefixes; empty = all events.
- `duration` (optional, default `30s`) — `<N>s`/`<N>m`/`<N>h` window.
- `max_events` (optional) — upper bound on frames returned.

### `get_sweep_status` (Phase C)

Return the `SweepInfo` for a single sweep plus up to N recent events
observed on its topics (default 10). The bus is in-memory and transient
— recent-events collection is a best-effort short subscribe window
(~200ms), not a replay log.

Inputs:
- `sweep_id` (required).
- `recent_events` (optional, default 10) — set to 0 to skip the
  subscribe window.

### `tail_sweep_log` (Phase C)

Read the last N lines of a sweep's per-sweep log file
(`.loom/logs/sweep-issue-<N>.log`). The log path is resolved from the
registry entry.

Inputs:
- `sweep_id` (required).
- `lines` (optional, default 100).

### `cancel_sweep` (Phase C)

SIGTERM → wait `grace` seconds → SIGKILL the sweep's **process group** (#3800,
#4980 — see "Process-group termination and the persisted `pgid`" above; the whole
subtree dies, not just the tracked leader). Transitions the registry entry from
`Running` to `Exited{code: None, at: now}` and releases the per-issue lock.
Idempotent: cancelling an already-terminal sweep returns success with
`was_running: false`.

Inputs:
- `sweep_id` (required).
- `grace` (optional, default 30) — seconds between SIGTERM and SIGKILL.

CLI equivalent: `loom-daemon cancel <sweep-id|--issue N>` (#4980) — same IPC
request, same daemon-side termination path, usable over ssh where no MCP server
is attached.

### `tail_event_bus` (Phase C)

Debug-oriented fire-hose subscription that streams ALL events on the bus
regardless of topic. Added per curator risk note D — multi-child
interactions are qualitatively harder to debug than hermetic children.

Inputs:
- `since` (optional, default `10m`) — `<N>s`/`<N>m`/`<N>h` streaming
  window. **Note**: the bus is transient — `since` is a streaming
  duration, not a backward-looking replay filter.
- `max_events` (optional) — upper bound on frames returned.

## In-memory registry layout

The sweep registry (`loom-daemon/src/sweep_registry.rs`) holds a
`BTreeMap<SweepId, SweepInfo>` keyed by stable IDs of the form
`sweep-issue-<N>-<unix-secs>` or `sweep-prs-<n1>-<n2>-...-<unix-secs>`.
`SweepInfo` carries:

- `sweep_id`, `kind` (`Issue(N)` or `PrSet(Vec<u32>)`), `pid`,
  `token_name`, `log_path`.
- `pgid` (optional, issue #4980) — the process group the child leads
  (`process_group(0)`, so it equals `pid` for a live dispatch). Persisted to the
  claim lock's `owner.json` too, so cancellation and crash-path reaping still
  reach the whole subtree after a daemon restart or from a fresh
  `loom-daemon cancel` process. `#[serde(default, skip_serializing_if =
  "Option::is_none")]`, so pre-#4980 wire data and clients remain compatible.
- `idempotency_key` (optional), `started_at`.
- `state` — one of `Pending`, `Running`, `Exited{code, at}`,
  `Crashed{at}`.
- `latest_phase` (optional) — most-recent phase advertised via
  checkpoint.
- `pr_number` (optional, reserved).
- `repo` (optional, issue #3929) — the owning managed-workspace root
  (`config.workspace_root`), stamped at dispatch/reconstruct time so a
  `list_sweeps` / `get_sweep_status` response disambiguates two managed repos'
  identically-numbered issues. `#[serde(default, skip_serializing_if = "Option::is_none")]`,
  so pre-#3929 wire data and clients remain compatible.

The wire shape is pinned by `sweep_info_schema_snapshot` in
`sweep_registry.rs` — a change to the JSON shape requires deliberate
test update.

## Per-workspace registry pool (`WorkspacePool`, #3928/#3929)

`loom-daemon/src/workspace_pool.rs` holds **one independent `SweepRegistry` per
registered repo root**, keyed by `PathBuf`, so every path a registry computes
(`.loom/locks/issue-<N>`, `.loom/logs/`, `.loom/sweep-checkpoint/`, and the
spawned child's `current_dir`) is namespaced per repo — two repos each with issue
#42 never collide. The default workspace's registry is **seeded** into the pool
(shared with the IPC `dispatch_sweep` path); the autonomous work-finder and epic
supervisor provision the other managed repos' registries on demand
(`get_or_provision`). The IPC handler resolves a request's target registry from
the pool when the request carries an explicit `workspace_root` (#3929), so all
per-repo registries are observable/addressable via `list_sweeps` /
`get_sweep_status` / `tail_sweep_log` / `cancel_sweep` / `dispatch_sweep`.

**Watchdog coverage for every provisioned workspace (#4124)**: `get_or_provision`
spawns the sweep watchdog (the startup-hang / mid-build-death / review-stall
self-healing backstops described above) alongside the reaper for **every**
pooled workspace, not just the default one — closing a gap where only the
default workspace's registry (wired up once in `main.rs`) had a watchdog and
every other managed repo got a reaper and nothing else, silently disabling the
startup-hang recovery from #3887 in any multi-repo deployment. Each pooled
workspace resolves its own watchdog config (enabled/timeout/interval/review-stall)
from its own `.loom/config.json`, mirroring the dispatch-stagger and quarantine
config already resolved per workspace — so the resolution is genuinely
per-workspace, not a copy of the default workspace's tuning. The seeded default
workspace is unaffected: `main.rs` still owns its one watchdog, and
`get_or_provision` returns the seeded entry without spawning a second one.

**Eviction on `workspace remove` (#3929)**: `DeregisterWorkspace` (the
`workspace remove` CLI) calls `WorkspacePool::evict`, which drops the pooled
registry and **aborts its background reaper and watchdog tasks** so neither
leaks. The **seeded default workspace is guarded** — it is owned by `main` and
keeps serving default-workspace IPC requests, so evicting it is a no-op. A live
sweep child in an evicted registry is **not** killed and its lock/log files are
untouched; only the in-memory tracking + reaper + watchdog go away, so the sweep
finishes normally but its terminal state becomes unobservable via IPC after the
deregister — an accepted consequence of an explicit operator `workspace remove`.

## Delegated daemon administration (`daemon.delegatedTo`, #5345)

On a host with several registered workspaces, the daemon is host-global
(one binary, one socket, one `~/.loom/workspaces.json`), but every
consumer repo still carries the full daemon-*administration* CLI surface
— `workspace add/set-priority/remove` and `tokens bootstrap` all happily
mutate that host-global state from whatever repo they run in. A repo can
opt out of that by declaring, in its own effective config (any tier
resolved by `config_resolver::resolve_effective_config`):

```json
{ "daemon": { "delegatedTo": "~/GitHub/fleet-repo" } }
```

Read via the single accessor `config_resolver::daemon_delegated_to(repo_root)`.
**Default-off**: with no `daemon.delegatedTo` key present (every repo's
config prior to this feature), every command below behaves exactly as
before.

**What is gated** (refuses with a message naming the delegate repo on
stderr, non-zero exit):

- `loom-daemon workspace add|set-priority|remove` — gated on the
  **invoking** repo (resolved from cwd, `repo_root::resolve_repo_root(".")`
  — the same resolution `calibrate`/`tokens select` already use).
- `loom-daemon tokens bootstrap --workspace <path>` — gated on the
  `--workspace` **target**, not cwd.
- The `workspace_unregistered` dispatch error's recovery hint
  (`DaemonError::workspace_unregistered`, surfaced when an explicit
  `workspace_root`/`--workspace` names an unregistered path) — checked
  against the **target** root's own delegation, not the invoker's, so it
  points at the delegate repo instead of suggesting `workspace add` be run
  locally.

**What is explicitly not gated** (client/read-only actions, unaffected by
`daemon.delegatedTo`):

- `loom-daemon workspace list` (read-only).
- `loom-daemon tokens select` (read-only, on the spawn hot path).
- `mcp__loom__dispatch_sweep`, `list_sweeps`, `get_sweep_status`,
  `subscribe_to_events`, `cancel_sweep` — every daemon-*client* action.
- `loom-daemon start|stop|update`/`launchctl`/`systemd` wrappers (shell
  scripts around `.loom/bin/loom`, not a `loom-daemon` subcommand — no
  single entry point to intercept) and `.loom/config.json`
  `autonomous.*` toggles (raw file edits, not a command) — both are
  advisory-documentation-only under this key, not enforced gates.

No bypass flag exists — the gate always reads the invoking/target repo's
own effective config.

## Fleet — operator-triggered multi-host worker fanout (`fleet`, #4340)

The `fleet` subcommand family is the operator-triggered path for running loom
across several hosts (epic #4340; architecture Option A — **federated daemons**,
one full `loom-daemon` per host, coordination stays label-based through the
forge, no new wire protocol). The **boundary decision** puts the fanout brain in
`loom-daemon` (`loom-daemon/src/fleet/`): when to expand, what a worker is,
dispatch/drain/teardown, fleet status. Generic VM provisioning stays in
`repo:remote` (rjwalters/repo) — the seam `fleet` consumes is "a reachable Ubuntu
box + an SSH alias", never a cloud CLI. v1 is operator-triggered, **not**
auto-elastic (no queue-depth/cost-cap triggers — deferred until the manual
command has mileage).

### The fleet registry & `LOOM_FLEET_PATH` (#5576)

Every `fleet` subcommand (`add-worker`, `status`, `drain`, `roll`) reads its
roster from one JSON file, resolved by `default_fleet_registry_path()`:
`~/.loom/fleet.json` by default, or the path in the `LOOM_FLEET_PATH`
environment variable when it is set and non-empty. **`LOOM_FLEET_PATH` is a
first-class, documented operator interface — not an internal test seam.**
Point it at a hand-crafted or externally-generated file to give the whole
family visibility into hosts `fleet add-worker` never touched:

- **macOS hosts** — `add-worker` refuses non-Linux targets outright (#5395;
  see the "no macOS/launchd path" note below), so a hand-onboarded Mac has no
  other way to become visible to `status`/`drain`/`roll`.
- **Pre-`add-worker` hosts** — a host provisioned before this fleet tooling
  existed, or by other means entirely.
- **Externally-provisioned hosts** — golden images, other orchestration
  tooling, anything that stands up a working `loom-daemon` without going
  through `add-worker`'s bootstrap plan.

The schema is `{ "version": 1, "workers": [ <WorkerRecord>, … ] }`. Every
`WorkerRecord` needs exactly two fields to parse: `ssh_host` (the SSH
alias/address `fleet` reaches it at) and `bootstrapped_at` (an RFC3339
timestamp — still required even for a record `add-worker` never wrote; use
whatever "added to the roster" timestamp makes sense, since the field name
predates this use case). Every other field (`repos`, `priority`,
`tailnet_name`, `provider_instance_id`, `added_by`, `state`,
`idle_shutdown_minutes`, `last_seen_up_at`, …) is `#[serde(default)]`/
`Option` and can be omitted.

Two correctness properties matter for any operator-supplied roster or
generator that produces one:

- **Joint ownership — the registry is a read/write file, not a read-only
  input.** `fleet status` writes `last_seen_up_at` back into whatever file
  `LOOM_FLEET_PATH` points at on every poll that observes a host `Up`; `fleet
  drain`/`add-worker` write their own fields (`state`, `drain_phase`,
  `drain_captured`, …) the same way. A generator that **overwrites the file
  wholesale on every run** will silently discard all of that observed state
  on its next regeneration — this was hit in practice generating a roster for
  a downstream fleet. If continuity of the daemon-written fields matters
  across regenerations, the generator must read the existing file first and
  carry forward any fields it does not itself own (merge, don't replace).
  Alternatively, treat `LOOM_FLEET_PATH` as a one-time seed and let `fleet`'s
  own commands own the file from then on.
- **Never include the local host.** The local host is always collected
  in-process (never over SSH) and rendered as its own `local` row — a roster
  entry whose `ssh_host` also names the local machine is redundant, and would
  otherwise reach it a second time over `ssh <self>`, which fails with
  "Permission denied" (nothing authorizes ssh-to-self). `fleet status` and
  `fleet roll --all` (the two commands that auto-enumerate the whole roster,
  as opposed to a single explicit `fleet drain <host>` / `fleet roll <host>`
  naming a host on purpose) filter this automatically: any `ssh_host` that
  matches a loopback alias (`localhost`, `127.0.0.1`, `::1`, `0.0.0.0`) or the
  local machine's own hostname (`$HOSTNAME`, then the `hostname` binary,
  case-insensitive, both full and short form) is excluded from the SSH fanout
  — surfaced as a notice in `fleet status`'s human output and on its `--json`
  `local_duplicates_filtered` field, rather than silently dropped. This match
  is best-effort (it does not resolve arbitrary SSH aliases/DNS), so avoid
  listing the local host explicitly regardless.

### `fleet add-worker <ssh-host> --repo <owner/name> [--repo …]` (#4341)

**Supported platform: Linux (Debian/Ubuntu) targets only** (#5395). Every step
below is built on `apt-get` and `systemd --user`, so before the plan is built
the command probes `uname -s` on the target over the same SSH connection and
**refuses a non-Linux host up front** — naming the platform (e.g. `mac-mini is
macOS (Darwin), but fleet add-worker supports Linux targets only`) rather than
failing opaquely partway through `base-deps`. Nothing on the host is touched by
that refusal. There is **no macOS/launchd path**: a Mac is onboarded by hand —
run `fleet add-worker <ssh-host> --repo <owner/name> --dry-run` (it never
contacts the host, so it works while pointed at a Mac) to print the ordered plan
as a manual checklist, then apply each step's intent with the platform
equivalents (Homebrew for `apt-get`, a `launchd` LaunchAgent for the
`systemd --user` unit + `loginctl enable-linger`, `launchctl kickstart -k` for
`systemctl --user restart`). A hand-built Mac is **not** in `~/.loom/fleet.json`
unless you add it, so `fleet status`/`fleet drain` will not see it.

Takes a reachable, already-provisioned host to "daemon running, workspace
registered, tokens ranked, dispatch verified" in one **idempotent** command over
`ssh <ssh-host>`. The bootstrap is modeled as an ordered **plan** of named steps,
each with a `check` (is it already done?) → `apply` → `verify` shape — Rust owns
the plan/ordering/checklist; the per-phase shell is rendered in
`fleet/add_worker.rs` (heredoc templates) and executed over a `CommandRunner`
(the production `SshRunner`, or a mocked runner in tests). The steps encode the
#3979 Phase-2 pilot's verified hand bootstrap:

1. **base-deps** — build-essential, pkg-config, libssl-dev, **libsqlite3-dev**
   (safehouse#38), git, gh. Deliberately does **not** install a Rust toolchain
   (#5067, Epic #4990 Phase 4) — the happy path below never needs one;
   `machine-layout` installs rustup itself, reactively, only on the fallback
   path.
2. **machine-layout** — clone loom → `~/.local/share/loom`, then install
   `loom-daemon` to `~/.local/bin` **from a GitHub Release artifact when one
   resolves for the host's platform** (#5067, Epic #4990 Phase 4), by
   shelling out to `loom-daemon-update.sh`'s own already-verified fetch +
   checksum-verify logic (`--no-restart`, since no daemon is running yet at
   this point in the plan) instead of duplicating it. Falls back to `cargo
   build -p loom-daemon --release` — installing rustup first, only if it
   isn't already present — only when no artifact resolves for this platform
   (unrecognized platform, no `gh` CLI, no Releases, a rate-limited/
   unreachable API, no matching asset). A host with **no Rust toolchain at
   all** goes from a bare provisioned OS to a running daemon purely from a
   Release artifact whenever one exists for its platform (Linux skips
   codesign either way).
3. **claude-code** — install the Claude Code CLI.
4. **forge-auth** — `gh auth login --with-token` with the operator's
   fine-grained PAT fed over **ssh stdin** (never a command line).
5. **token-accounts / token-pool / token-ranking** — install `accounts.env`
   (0600, over stdin), `loom-daemon tokens bootstrap --shared`, then `tokens
   check --ranking`. The **full** account pool ships (per #3979 — no pinned
   subsets).
6. **workspace-clone** — `gh repo clone` each `--repo` + `loom-daemon init`
   (installs the `/loom:sweep` command, #4027).
7. **workspace-register** — `loom-daemon workspace add` each repo at `--priority`.
8. **daemon-unit** — a systemd `--user` unit (`Restart=on-success`,
   `Environment=LOOM_DAEMON_SUPERVISOR=systemd`, `loginctl enable-linger`) —
   mirroring the canonical `render_systemd_unit()` policy in
   `loom-daemon-start.sh` (#4268) so `restart --drain` (#4090) works on fleet
   workers (#4640). `WorkingDirectory=` is pinned to a workspace clone as the
   **#4292** token-pool-cwd workaround — the rendered unit carries a `#4292`
   marker so it is removed when that lands.
9. **idle-shutdown** (optional, `--idle-shutdown-minutes N`) — a cron guard that
   powers the host off after N idle minutes. This is stage 2:
   `autonomous.idleExit` is stage 1. On daemon-managed hosts use a short guard
   window (typically 15–30 minutes).

   **The guard vetoes on the daemon's own reported eligibility, not on bare
   process presence (#5565).** Under the fleet's own `daemon-unit` systemd
   `Restart=on-success` supervision (step 8 above), a stage-1 `autonomous.idleExit`
   self-exit is respawned immediately — so a veto on `pgrep -f loom-daemon`
   (the pre-#5565 behavior) could never observe an absent daemon, making
   `--idle-shutdown-minutes` a silent no-op on every `fleet add-worker`-provisioned
   host. The guard now calls `loom-daemon status --json` and reads its
   `idle_exit.eligible` field — the SAME 0-in-flight / no-active-role /
   no-lifecycle-activity-within-the-window (or token-starvation) determination
   `autonomous.idleExit`'s own tracker computes — and vetoes on *that* instead. A
   daemon too old to report eligibility (no `idle_exit` block on the wire) falls
   back to the raw in-flight-sweep count; a genuinely unreachable daemon (socket
   gone) contributes no veto of its own, since there is nothing left to ask.

   **This is a real power-off, not a suspend** — the box goes fully dark (no
   SSH, no tailnet, nothing) until an operator brings it back (#4697). Wake
   path:
   - **Power the host back on** via the provider's own console or CLI. Loom
     deliberately never calls a cloud CLI itself here (the "Boundary" note
     above/epic #4340's scope) — this is an operator (or `repo:remote`) action,
     not something `loom-daemon` automates.
   - **Tailnet identity**: a Tailscale node's identity/key is stored on local
     disk (`/var/lib/tailscale` or the equivalent state dir), not tied to the
     instance being *running* — a plain power-off/power-on (not a
     re-image/re-provision) preserves it, and the host reappears on the
     tailnet under its existing name/IP once `tailscaled` starts again at
     boot, typically within a minute or two of the power-on. A re-imaged or
     freshly-reprovisioned replacement host does **not** inherit this and
     needs its own `tailscale up` (a fresh ephemeral auth key if the prior one
     already expired/was revoked).
   - **Re-registration**: none needed for a simple power-cycle of the same
     box — the fleet registry (`~/.loom/fleet.json`) keys on the SSH alias/
     host, which is unchanged, and `loom-daemon` + the idle-shutdown cron
     guard restart on boot exactly as they did before the power-off (the
     systemd `--user` unit + `loginctl enable-linger` from step 8 survive a
     reboot by design). Re-run `fleet add-worker` against the host only if the
     box was actually replaced/re-imaged (a fresh OS install has none of the
     prior bootstrap state).
   - **Operator visibility**: `fleet status` (#4342, extended by #4697)
     distinguishes a host that is silently past its configured
     `--idle-shutdown-minutes` window — reported as `POWERED OFF`,
     not a bare `UNREACHABLE` — from one whose silence doesn't yet explain
     itself, which stays `UNREACHABLE` (genuinely worth investigating). This
     classification is read from the registry's persisted
     `idle_shutdown_minutes` + `last_seen_up_at` fields; it is advisory
     (still counts as non-`Up` for `fleet status`'s exit-code policy) and
     never suppresses the row.
10. **safehouse** (optional, `--safehouse`) — **skip-with-notice** until #3998
    (the safehoused provisioning fragment) lands.
11. **verify** — `loom-daemon status` sane from the workspace cwd, ranking
    fresh, workspace registered.

Steps landed since the pilot are **deliberately absent**: no Python `loom_tools`
/ `pip --break-system-packages` (native token selection landed #4228), no
single-repo daemon-cwd pin for dispatch (registry-resolved dispatch landed #4299
/ PR #4322).

**Secrets** (`--pat-file`, `--accounts-env`) are read locally at **preflight**
(a missing/empty file fails before any remote action) and travel to the worker
only over ssh stdin — never a command line, never a logged rendered script. A
supplied secret is `StepStdin { secret: true }`, redacted in dry-run output and
`Debug`.

`--dry-run` prints the full ordered plan without contacting the host — which
means it also skips the platform probe above (host contact), so it prints the
plan plus a reminder that a real run is Linux-only. On a
successful run each worker is recorded (dedup on the SSH alias) in a machine-level
**fleet registry** at `~/.loom/fleet.json` (`LOOM_FLEET_PATH` override) — the
inventory the siblings `fleet status` (#4342) and `fleet drain` (#4343)
enumerate. A re-run is idempotent: each step's `check` reports it `unchanged`.
The registry's `WorkerRecord` also carries the canonical roster fields an
operator otherwise tracks by hand: `provider_instance_id`, `tailnet_name`,
`added_by`, and a lifecycle `state` (`fleet drain`'s `"draining"` sentinel,
#4343) — all `#[serde(default)]`/`Option`, so an older registry file keeps
parsing.

**Retrofitting a worker provisioned before #4640**: an older `daemon-unit` step
rendered `Restart=on-failure` with no `LOOM_DAEMON_SUPERVISOR` at all, so
`loom-daemon restart --drain` refuses on it ("no supervisor detected") even
though systemd IS managing it. Either re-run `fleet add-worker` against the
host (the `daemon-unit` step overwrites the unit unconditionally via `cat >`,
so a re-run fully re-syncs it), or apply a drop-in by hand over ssh without a
full re-provision:

```bash
mkdir -p ~/.config/systemd/user/loom-daemon.service.d
printf '[Service]\nEnvironment=LOOM_DAEMON_SUPERVISOR=systemd\nRestart=on-success\n' \
  > ~/.config/systemd/user/loom-daemon.service.d/supervisor.conf
systemctl --user daemon-reload
```

A systemd drop-in's `Environment=` is additive and its `Restart=` overrides the
base unit, so this one file fixes both defects (the missing supervisor env and
the wrong restart policy) without touching the rendered base unit.

### `fleet bootstrap-spice <ssh-host>` (#4931, Phase 1a)

Provisions a **pinned SPICE simulation toolchain** (ngspice + Xyce, built from
source) plus the **gf180mcu and sky130 open PDKs** onto an already-reachable SSH
host, so analog-repo simulation load can be offloaded off the agent fleet's
primary host. Same seam as `add-worker`: it consumes "a reachable Ubuntu box + an
SSH alias" (from `repo:remote` or operator-supplied) and never wrangles a cloud
CLI.

**A sim runner is not a loom worker.** `spice_runner.rs` is a *sibling* of
`add_worker.rs`, not a profile of it: it reuses the same `Plan`/`Step`/
`CommandRunner` machinery (and `add_worker::SshRunner` directly) but touches
**no** cloud CLI, **no** Tailscale API, and **no** forge/token credentials — no
`gh auth`, no `accounts.env`, no token pool, no safehouse. Every clone is an
anonymous HTTPS clone of a public repo. It also does **not** write
`~/.loom/fleet.json`: that registry models hosts running `loom-daemon`, and
adding a sim-only box would make `fleet status` poll a host with no daemon to
answer.

**Ordered plan** (7 entries, each `check` → `apply`):

| # | Step | What it does |
|---|------|--------------|
| 1 | `spice-base-deps` | apt: build-essential, bison/flex, cmake, gfortran, BLAS/LAPACK/SuiteSparse, X11/readline headers |
| 2 | `spice-path` | append the canonical loom PATH (#4831) to `~/.profile` so `~/.local/bin` resolves for interactive logins |
| 3 | `ngspice` | clone + `configure --prefix=~/.local --enable-xspice --enable-cider` + `make install` at the pinned ref |
| 4 | `xyce` | build Trilinos (Xyce's documented option set) then Xyce against it; **skipped** with `--skip-xyce` |
| 5 | `gf180mcu-pdk` | checkout `google/gf180mcu-pdk` at the pinned ref + init the device-model submodule |
| 6 | `sky130-pdk` | checkout `google/skywater-pdk` at the pinned ref + init the device-model submodule |
| 7 | `verify` | binaries on PATH, both PDK checkouts present with non-empty model libraries |

Everything lands under `~/.loom/spice/` (`src/`, `pdks/`, `stamps/`,
`trilinos-install/`); the binaries install to `~/.local/bin`.

**Idempotency is version-aware.** Unlike `add-worker`'s steps (which probe for a
binary's mere presence), each toolchain/PDK step compares a **ref stamp**
(`~/.loom/spice/stamps/<name>.ref`, written by that step's `apply` only *after* a
successful install) against the currently-configured pin. So:

- Re-running at the **same** pins reports every step `unchanged` and touches
  nothing.
- Bumping **one** pin (e.g. `--ngspice-ref`) rebuilds only that step; the Xyce
  stamp covers the Trilinos pin too, so bumping either rebuilds Xyce.
- A build that dies half-way leaves no stamp, so it is never mistaken for a
  completed one — and because every step is `check`-guarded, a re-run after
  fixing the cause skips the satisfied prefix and **resumes at the failure**
  (same guarantee as `add-worker`).

**Pins and overrides.** The defaults are explicit refs (never `latest`/`main`) —
ngspice `ngspice-42`, Xyce `Release-7.7` + Trilinos `trilinos-release-14-4-0`,
gf180mcu-pdk `v0.9.5`, skywater-pdk `v0.0.3` — each overridable via
`--<tool>-ref` / `--<tool>-repo-url`. The PDK repos keep their device models in
submodules, so only the configured models submodule is initialized
(`--gf180mcu-models-path` / `--sky130-models-path`, empty ⇒ top-level clone
only) rather than the tens of GB of standard-cell libraries a SPICE runner never
reads. Every ref/URL/path is charset-validated at preflight before it is
rendered into shell.

`--skip-xyce` renders the Xyce step as a visible `SKIP` entry (Xyce's
from-source build with Trilinos is measured in hours; analog repos that only
simulate with ngspice should not pay for it).

```bash
# Print the ordered plan without contacting the host.
loom-daemon fleet bootstrap-spice sim-runner-1 --dry-run

# ngspice-only runner, custom pin.
loom-daemon fleet bootstrap-spice sim-runner-1 --skip-xyce --ngspice-ref ngspice-43
```

**Out of scope here** (deliberately, per #4931's phasing): the `spice-run`
dispatch wrapper that routes a sim local-vs-remote lives in the analog repos
(Phase 1b), result caching keyed on netlist/deck/corner is Phase 2, and
scale-up/scale-to-zero elasticity is Phase 3.

### `fleet status [--json] [--timeout-secs <SECS>]` (#4342, #5575)

Aggregates sweep/token/health state across **every** fleet host, side by side,
in one command — the roster + SSH-fanout + merge/render layer over the status
IPC `loom-daemon status --json` already provides (#4069); no new status wire
format was needed.

- **Local host**: always included as its own row (`local`), collected
  in-process over the daemon's own Unix socket — never `ssh localhost`.
- **Remote hosts**: enumerated from the fleet registry, collected **concurrently**
  over `ssh -o BatchMode=yes -o ConnectTimeout=<N> <host> 'loom-daemon status
  --json'`, each bounded by a per-host `tokio::time::timeout` (default 8s) so one
  hung host cannot stall the report. `--timeout-secs` (#5575) raises or lowers
  both the `ConnectTimeout` and the outer per-host bound together (one combined
  knob, not split) — a worker running several in-flight sweeps can legitimately
  take longer than 8s to answer `status --json` over ssh (it is enumerating
  in-flight sweeps over its own daemon socket while they run); that is normal
  loaded behavior, not a hang, and the default bound alone would misreport such
  a host as `UNREACHABLE`. Omitting the flag preserves the pre-#5575 8s default
  exactly.
- **Per-host state** (loud and distinct — silence must never read as idle):
  - `UP` — the host answered with a well-formed status payload.
  - `DAEMON DOWN` — the host answered, but its own daemon reports the #4069
    unreachable-daemon payload (still valid JSON, carries an `error` key).
  - `UNREACHABLE` — SSH/connect failure, or the per-host timeout elapsed, AND
    (#4697) either no idle-shutdown guard is configured for this host or its
    silence hasn't yet reached the configured window — genuinely worth
    investigating.
  - `POWERED OFF` (#4697) — the host is `Unreachable` by every
    signal available (SSH/connect failure or timeout — an idle-shutdown
    power-off looks no different over the wire than a genuinely dead host),
    **but** it has a configured idle-shutdown window
    (`WorkerRecord.idle_shutdown_minutes`, populated at `fleet add-worker
    --idle-shutdown-minutes` time) and its elapsed silence since the last
    confirmed-`Up` poll (`WorkerRecord.last_seen_up_at`, refreshed by `fleet
    status` itself on every poll that observes the host `Up`) has already
    reached that window. Read as "likely powered off as designed, not a
    failure" — still counts as non-`Up` for the exit-code policy below (it is
    not confirmed-alive), but is loudly distinguished from a genuine outage so
    an operator doesn't chase a phantom incident. The "expected, not a
    failure" qualifier and the wake pointer ride on the row's `->` detail line
    and the `N powered-off (expected)` summary tally rather than the STATE
    cell, which stays inside the table's column width. Deliberately conservative:
    either input missing (no guard configured, or the host has never been
    observed `Up` by `fleet status` yet) leaves the host `UNREACHABLE`, the
    pre-#4697 default — a false "expected, don't worry" reading on a
    genuinely dead host is worse than an occasional false alarm. See the
    `fleet add-worker` idle-shutdown step above for the wake path.
  - `PARSE ERROR` — the payload could not be parsed as JSON at all (severe
    version skew). Parsing is otherwise **lenient**: the remote payload is kept
    as a raw JSON value, so an older/newer remote binary's reduced/extended
    field set renders missing columns as `–` rather than failing the row.
  - `DRAINING` — the registry's `state: "draining"` (written by `fleet drain`'s
    first phase, #4343): rendered distinctly, without an SSH probe at all (a
    control-plane fact, not a liveness one) — an interrupted drain stays
    visible rather than looking like a parse error.
- **`safehoused` presence**: a cheap best-effort probe (socket / `pgrep`);
  degrades to `unknown` rather than erroring the row.
- **Empty roster**: never renders as empty output — prints an explicit "no
  fleet workers registered" notice alongside the local host's row. #5060: an
  empty (or effectively-empty, i.e. only the always-present local row)
  registry is **not** a healthy fleet reading — see the exit-code carve-out
  and summary-line wording below. Registered-fleet visibility only covers
  what has actually been `fleet add-worker`'d; a self-maintained host that is
  never registered is invisible to every `fleet status` run on every other
  host, not just flagged unhealthy — this is why the incident that motivated
  #5060 went unnoticed for hours despite one of the unregistered hosts being
  genuinely broken. **Guidance**: any host that is meant to be part of this
  fleet's cross-host visibility (i.e. anything other than a pure local dev
  checkout) should be registered via `fleet add-worker` from at least one
  other host's registry; an unregistered-but-live self-maintained daemon is
  itself worth treating as a gap, not an acceptable steady state. A soft
  cross-check that surfaces unregistered-but-reachable peers (e.g. by probing
  the tailnet or a known-hosts list) was considered but is out of scope here
  — track it separately if still wanted.
- **Exit code**: `0` only when the roster is non-empty **and** every roster
  host is `UP`; non-zero otherwise (a monitor/CI check should treat any
  non-zero exit as "go look" — including a `POWERED OFF` host, since it is
  still not confirmed-alive). #5060: an empty roster (nothing but the local
  row) always exits non-zero, even though that lone local row is itself
  `UP` — a 1-of-1 "everyone present is up" reading is not the same claim as
  "the fleet was checked and is healthy," and a watch loop must not be able
  to read an empty-roster run as an all-clear. The human-readable summary
  line is likewise qualified for this case (`EMPTY ROSTER — N of N known
  host(s) up (...); this is roster coverage (local host only), NOT a
  confirmed healthy fleet.`) rather than the unqualified `N host(s): N up,
  ...` line used for a real, populated roster — a reader skimming just the
  last line, not the notice above the table, still sees the distinction. The
  `--json` `summary.empty_roster` field already existed before this fix and
  is unchanged; only the exit code and the human summary line now consult it.
- **`--json`** schema: `{ "hosts": [ { "alias", "state", "tailnet_name"?,
  "provider_instance_id"?, "added_by"?, "is_local", "workspaces", "status"?,
  "detail"?, "safehoused" } ], "summary": { "total", "up", "daemon_down",
  "unreachable", "powered_off", "parse_error", "draining", "empty_roster" },
  "local_duplicates_filtered"? }` — `local_duplicates_filtered` (#5576, an
  array of `ssh_host` strings, omitted entirely when empty) is present only
  when the roster contained an entry recognized as the local host itself (see
  "The fleet registry & `LOOM_FLEET_PATH`" above) — treat this as a consumed
  interface (the #4329 dashboard's multi-host phase reads it over the
  tailnet).

### `fleet drain <ssh-host> [--timeout N] [--force-after-timeout] [--json]` (#4343)

Retires a worker without losing in-flight work, forge claims, or (when a real
flush seam lands — see below) E2E room keys — the SSH-orchestration layer over
the drain engine that already exists (`restart --drain`, #4090; `SweepRegistry::
cancel`'s SIGTERM→grace→SIGKILL), plus the three deltas teardown needs:

1. **Drain-then-*exit*, not drain-then-restart.** `restart --drain --then-exit`
   (this issue's addition to `Restart`/`DrainAndRestartDaemon`) tells the remote
   daemon to end the process and **stay down** (`EXIT_SHUTDOWN`, no supervisor
   relaunch) instead of restarting once drained — a relaunched daemon could pick
   up new dispatch before the box powers off, defeating the whole point of
   draining. Because a `then-exit` drain never wants a relaunch, the
   supervisor-detection refusal gate (`restart --drain`'s AC5) does not apply to
   it.

   **`then_exit` is escalated one-way, and the ack reports reality (#4521).**
   A drain request that lands while a drain is *already* in progress is
   idempotent — it never stacks a supervisor and never moves the active
   deadline — but `then_exit` is not simply ignored: `--then-exit` **escalates**
   an in-progress relaunch-drain (e.g. the auto-update roll, which drains with
   `then_exit=false`) to stay-down, and the already-running supervisor observes
   the change because it re-reads the terminal action from the drain descriptor
   on every poll. The reverse never happens: a plain `--drain` against an active
   teardown drain does **not** downgrade it back to a relaunch. Escalation was
   chosen over "refuse and tell the operator to `--abort-drain` + re-issue"
   because that two-command path races the very window it exists for — the roll
   can complete between the abort and the re-issue and relaunch the daemon on a
   host that is about to be powered off. When a roll is escalated this way, the
   daemon logs that the rebuilt binary will not be picked up until it is started
   again.

   Consequently the `DaemonDrain` reply's `then_exit` reports the **active**
   drain's terminal action, never an echo of the request, and
   `loom-daemon restart` renders "will stop" vs "will RESTART" from the reply.
   When the reply disagrees with what was asked, the CLI prints a loud warning —
   this is also the version-skew detector: a daemon older than `--then-exit`
   support silently drops the unknown wire field and replies `then_exit: false`,
   so `--drain --then-exit` against a stale incumbent now says so instead of
   promising a teardown that never happens (roll the daemon, then re-issue).
2. **Immediate, targeted claim reset.** The startup-only `claim_reconciliation`
   pass is too late for a drain (the anvil#758 pilot evidence: a crashed VM
   sweep stranded `loom:building` for the full staleness window). `fleet drain`
   captures the worker's in-flight issue numbers (`status --json`) *before*
   triggering the remote drain, then — once the remote daemon has exited —
   flips any of them still `loom:building` back to `loom:issue` via `gh`,
   **locally, never over SSH** (the forge is global). An issue that finished
   normally in the meantime (no longer `loom:building`) is left alone.
3. **Safehoused flush verification is a documented stub pending #3998.** No
   invocable key-backup steady-state check exists in this repo yet (only the
   narration-sink/peer-claim client seams in `safehouse.rs`) — rather than block
   every drain on #3998, this phase degrades honestly: `safehouse.enabled ==
   false` skips cleanly (no room keys in play); `safehouse.enabled == true`
   still lets the drain complete (workspace/roster cleanup proceed — loom never
   *refuses* to retire a box over this) but withholds "safe to power off" and
   exits `3`. TODO(#3998): replace the stub with a real check once a seam lands.

**Phase state machine** (`loom-daemon/src/fleet/drain.rs`), idempotent +
resumable (an interrupted drain re-runs from its last completed phase, persisted
on the registry entry as `drain_phase`):

`mark-draining` (roster `state: "draining"`, first — crash-safety) →
`capture-claims` → `trigger-remote-drain` → `wait-remote-exit` →
`reset-claims` → `flush-safehouse` → `deregister-workspace` →
`remove-from-roster` (last — an interrupted drain stays visible, never
silently gone).

**What `wait-remote-exit` counts as "exited"**: the normal end state of a
`then_exit` drain is **host up, daemon gone**, in which the remote
`loom-daemon status --json` still answers — with the #4069 unreachable payload
(`{"error": "could not reach loom-daemon at …", "install_state": …}` on stdout,
non-zero exit). That payload, and an SSH-level failure (host itself gone), both
mean "exited". Only a *live* status payload reporting `drain.draining: false`
means the remote refused the drain and is still dispatching — the fail-loud
exit-`2` case. A transient SSH blip mid-wait is indistinguishable from "host
gone" and reads as a successful exit; the following phases are
orchestrator-side and a still-running remote daemon shows up in the next
`fleet status`.

**Exit codes**: `0` fully verified (safe to power off); `1` a phase failed
outright (SSH/launch failure, remote refusal — re-run to retry, resuming from
the persisted phase); `2` the remote drain timed out **without**
`--force-after-timeout` (fail-safe refusal, remote daemon still up); `3` every
phase completed but the safehoused flush is unverified (#3998).

**Boundary (epic #4340)**: loom never calls a cloud CLI. The final report
prints the exact `repo:remote --down <ssh-host>` teardown command for the
operator to run — this command hands the box back, it does not execute it.

### `fleet roll [<ssh-host>|--all] [--timeout N] [--json]` (#5504)

Rolls the `loom-daemon` binary across fleet hosts with a **measured** success
verdict — SSH orchestration over the existing `defaults/scripts/cli/
loom-daemon-update.sh` (invoked from the canonical `$HOME/.local/share/loom`
machine checkout, the same way `fleet add-worker`'s machine-layout step does),
plus the pre/post verification this issue exists for. Exactly one of
`<ssh-host>` or `--all` (every host in the fleet registry) must be given.

**The insight**: neither `loom-daemon --version` nor the update script's own
exit code/transcript is trustworthy evidence that a roll took. `--version`
**execs the binary**, so it reports whatever was last *built* even when the
running process is hours older and still serving the old code (#5467); the
update script printed `RELAUNCH NOT CONFIRMED` on one host that had, in fact,
succeeded (#5390). The one reliable test is comparing the **process start
time** (`systemctl --user show loom-daemon.service -p ActiveEnterTimestamp` on
Linux, `launchctl` + `ps -o lstart=` on macOS) against the **binary build
time** (the installed binary's mtime): if the process predates the binary, the
restart did not take, regardless of what anything else reports.

**Per-host sequence** (`loom_daemon::fleet::roll::execute_roll`):

1. `--check` (cheap, no writes): does the installed binary already match
   source HEAD?
2. A pre-roll probe measures the *running process's* start time vs the
   binary's build time — this is what catches the #5467 case where the binary
   is current but the process silently never picked it up.
3. If the binary is current AND the process already reflects it: `ALREADY
   CURRENT`, no further remote calls. If the binary is current but the process
   is stale (the #5467 bug), the update script runs with `--force` to force a
   restart even though nothing needs rebuilding. Otherwise it runs with no
   flag (a normal, needed rebuild).
4. A post-roll probe re-measures the same comparison — this, not the script's
   exit code, is the final verdict: `ROLLED` (process now at/after the
   binary's build time) or `FAILED` (still stale, or a timestamp could not be
   measured/parsed — never silently folded into success).

**Interrupt safety (#5340's lesson, generalized)**: once the update step is
launched on a host, it is never signaled for any reason, including a local
timeout — the binary may already be swapped on disk with the old process still
serving requests, and interrupting there is worse than a slow roll. `--all`
fans a host out concurrently, each bounded by `--timeout` (default 1800s)
mirroring `fleet status`'s "one hung host cannot stall the report" —  but a
timeout there only stops the orchestrator **waiting**; it never kills the
underlying SSH child, and the host reports `UNREACHABLE` (measured-uncertain,
not a claimed success or failure).

**Exit codes**: `0` only when every targeted host is `ROLLED` or `ALREADY
CURRENT`; non-zero if any host is `FAILED`/`UNREACHABLE`, or `--all` was given
against an empty fleet registry (mirrors `fleet status`'s #5060 "empty roster
never reads as healthy" policy).

## Token pool provisioning for managed repos (#3938)

The multi-workspace work finder measures the token pool **once per tick from the
daemon's primary workspace** (`fallback_root`) and uses it as the single global
concurrency budget — token accounts are a *machine-level* resource, so the cap is
never replicated per repo. But each dispatched sweep runs `spawn-claude.sh` with
its `current_dir` set to **its own** repo root, and `spawn-claude.sh` resolves the
token pool from that repo. A freshly-installed **consumer repo has no
`.loom/tokens/` of its own** — only the primary workspace was bootstrapped — so
every cross-repo dispatch used to hard-fail instantly with `EX_CONFIG`
("Run `loom-daemon tokens bootstrap` …"), burning a dispatch slot per tick on children
that died in ~2s.

**Fix — a shared machine-level pool with a per-repo fallback.** Token selection
and *all* pool-state bookkeeping resolve the effective pool directory as:

1. the **per-repo** pool `<repo>/.loom/tokens/` when it holds `*.token` files
   (unchanged for the primary workspace);
2. else the **shared** machine-level pool `~/.loom/tokens/` (override
   `LOOM_SHARED_TOKENS_DIR`; set it empty to disable the fallback);
3. else the per-repo path (so a truly-unbootstrapped repo still surfaces a clear
   "run bootstrap" error).

Crucially, the **state files** (`.bad_tokens`, `.failure_counts`, `.ranking`,
`.allowlist`) are read/written in *whichever pool directory was selected* — so a
consumer repo dispatching against the shared pool shares one `.bad_tokens` /
`.ranking` truth with every other repo. Pool state is **never forked per repo**,
which is what keeps the token-capacity backpressure accounting (#3907/#3930)
consistent. The Rust `token_pool_size` (the dynamic-cap input) applies the same
per-repo→shared resolution, so the daemon's concurrency ceiling matches what the
spawn path can actually pick.

**Provisioning a managed-repo pool.** Bootstrap the shared pool once per machine:

```bash
# Preferred on a host running claude-monitor — reads the live credential store
# (~/.claude-monitor/usage.db -> oauth_credentials, opened mode=ro), so no
# accounts.env is needed on this machine at all:
loom-daemon tokens import-from-monitor --shared   # writes ~/.loom/tokens (override LOOM_SHARED_TOKENS_DIR)
loom-daemon tokens check --ranking                # ranks the effective pool (shared when no per-repo pool)

# Without claude-monitor — materialize from the accounts.env snapshot instead:
loom-daemon tokens bootstrap --shared      # writes ~/.loom/tokens (override LOOM_SHARED_TOKENS_DIR)
loom-daemon tokens check --ranking         # ranks the effective pool (shared when no per-repo pool)
```

Every consumer repo the daemon dispatches into then falls back to that one pool —
no per-repo `loom-daemon tokens bootstrap` required. A repo that *wants* its own isolated
pool can still `loom-daemon tokens bootstrap` locally; the per-repo pool always wins.
Selection sources (`~/.claude-monitor/accounts.env`, repo-local `.env`) are
unchanged for `bootstrap` — `--shared` only redirects the *destination* of the
materialized pool. `import-from-monitor` bypasses `accounts.env` entirely and
takes claude-monitor as authoritative for pool membership (use `loom-daemon tokens pin`
to restrict which accounts the selector may pick). If accounts later go
`blocked` on revoked tokens, `bootstrap --force` cannot recover — the
`accounts.env` snapshot is itself what went stale — run
`loom-daemon tokens import-from-monitor --force && loom-daemon tokens check --ranking`
instead; without `--force` a rolled token is reported as drift and left alone,
and the command exits `2`. See "Importing live tokens from claude-monitor" in
the root `CLAUDE.md` for the full behavior.

### `status` reports the resolved pool directory, not a cwd-derived guess (#4292)

Every surface that reads or writes the token pool — `loom-daemon status`, `tokens
select` / `check` / `pin` / `unpin` / `unblock` / `mark-bad`, and the daemon's own
token-ranking self-refresh loop (`autonomous.tokenRankingRefresh`, above) —
resolves through the **same** precedence (env override `LOOM_SHARED_TOKENS_DIR`
disables/redirects the shared fallback; otherwise per-repo
`<workspace>/.loom/tokens/` wins when it holds `*.token` files, else the shared
`~/.loom/tokens/`). Before #4292, `loom-daemon status` computed its per-token usage
table **client-side** from the invoking process's own `cwd`
(`resolve_tokens_workspace(".")`), independently of the pool directory the
*daemon* itself resolved for `token_pool_size` / the dynamic-cap accounting.
Running `status` from a different repo checkout than the daemon's own primary
workspace could therefore report a false token picture (e.g. `0/0 healthy`) even
though the daemon's own pool was perfectly healthy.

The `DaemonStatusReport` now carries `token_pool_dir` — the exact directory the
daemon resolved server-side — and `status` prints it (`pool: <dir>` in the human
view, `dynamic_cap.token_pool_dir` in `--json`) and probes per-token usage against
*that* directory instead of re-deriving one from the CLI's own cwd. The net effect:
**`loom-daemon status` reports the same token picture no matter which directory it
is run from** — it always describes the daemon's actual pool, never a client-side
guess. `token_pool_dir` is `null` only when talking to a pre-#4292 daemon binary.

**systemd note.** `loom-daemon-start.sh`'s generated unit (#4260/#4268) always sets
`WorkingDirectory=` to a real resolved repo root (`LOOM_MACHINE_CHECKOUT` in
machine mode, else `find_repo_root()`), so the daemon's primary workspace is never
an incidental cwd when started that way. A **hand-rolled** unit that omits
`WorkingDirectory=` starts with whatever cwd the service manager happens to use
(often `~`) as the primary workspace instead — as of #4292 (trip-wires 1 & 3,
completing this issue) that no longer needs a `WorkingDirectory=` override to
find its pool: dispatch-capacity accounting, `loom-daemon status`, and
`tokens check --ranking`'s default `--workspace` all now ask whether that
seeded primary workspace is itself a *recognized* Loom workspace (registry
membership, reusing the #4299 check) before applying the per-repo/shared
`#3938` precedence — and when it is not (the bare-`$HOME` case), they anchor
straight to the shared machine-level pool instead of a per-repo(`$HOME`) path
that can coincidentally collide with the shared *default* and mask wherever
the pool was actually bootstrapped. The operational contract is unchanged:
always provision a machine-level daemon's pool with `loom-daemon tokens bootstrap
--shared` (or `import-from-monitor --shared`) — which always targets
`~/.loom/tokens/` regardless of cwd — so the daemon's anchoring and the
provisioning step agree, whether or not the unit that starts the daemon sets
`WorkingDirectory=`. Full precedence chain: `.loom/docs/token-pool.md` →
"Full anchoring precedence, and machine-level daemon startup (#4292)".

## Per-repo status breakdown + per-repo main-health gate (#3930 — phase d)

Phase d is the final phase of the multi-repo daemon (#3926/#3835). Phases b/c
already delivered the single global concurrency budget, per-workspace error
isolation, and `(repo, issue)`-aware IPC/events. Phase d closes the two remaining
gaps: making `loom-daemon status` see *every* managed repo, and making the
reactive main-health gate **per-repo** instead of one flag gating all repos.

### Per-repo status breakdown (AC1)

`build_daemon_status` (`ipc.rs`) enumerates
`WorkspaceRegistry::effective_roots(&fallback_root)` (an **empty** registry ⇒ the
single daemon workspace, byte-for-byte the pre-#3930 view) and reads each root's
own registry from the `WorkspacePool` (`get_or_provision`). It returns:

- `DaemonStatusReport.in_flight` — now the **union** of non-terminal sweeps across
  every registered repo, so a sweep the autonomous loops dispatched into a
  non-default managed repo is finally visible in `loom-daemon status`.
- `DaemonStatusReport.per_repo: Vec<RepoStatus>` — one entry per root with `root`,
  `in_flight_count`, and `health_gate_halted`. Additive + `#[serde(default)]`, so
  pre-#3930 JSON consumers round-trip unchanged (an absent `per_repo` deserializes
  to an empty vec).

`loom-daemon status` prints a **Managed repos** section (root, in-flight count,
gate state); `loom-daemon status --json` adds the `per_repo` array. The
dynamic-cap inputs (disk headroom, ram headroom, configured ceiling) remain
computed once from the daemon's primary workspace —
they are *machine-level* resources, so
they stay a single global figure, not per-repo. `resolve_registry` (the
per-request `workspace_root` targeting used by `dispatch_sweep` / `list_sweeps` /
etc.) is unchanged: the cross-repo aggregation is a read-only snapshot for
`DaemonStatus`. `list_sweeps` gained its own opt-in fleet-wide fan-out
(`all_workspaces`, issue #6006 — see the `list_sweeps` reference above) so a
merged view across every registered repo no longer requires one call per repo.

### Per-repo main-health gate (AC2)

The reactive gate was single-repo, single-flag: one `MainHealthState` driven by
one gate check against the daemon's own workspace, applied uniformly to every
registered repo's dispatch (a red `main` in one repo halted all repos; a red
`main` in any *other* registered repo was never even checked). Phase d replaces
the single flag with `WorkspaceHealthStates` — a `HashMap<PathBuf,
Arc<MainHealthState>>` keyed by normalized root (mirroring `WorkspacePool`'s
keying) — and adds `spawn_multi_main_health_gate_task`, which each cycle:

1. Re-reads `effective_roots(&fallback_root)` (hot-applies `workspace add|remove`).
2. Runs **one gate check per registered root**, resolving that root's own
   enablement (`autonomous.mainHealthGate.enabled`, env > config > default) and
   its own `buildGate` block — no new config schema. A root that is
   disabled / has no `buildGate` block is treated as **always-green** (its halt
   flag is cleared and no command runs). Gates run **sequentially** per tick, so
   several minutes-long per-repo builds firing together never contend (each
   `CommandGateRunner` already isolates its own `origin/main` sync + uuid temp
   file).
3. Applies each outcome to that root's own `MainHealthState`.

`work_finder::tick_multi` now takes a `halted: &[bool]` slice parallel to the
workspaces, so a **red repo skips only its own dispatch loop** (its backlog is
still counted in `seen` for logging, and its in-flight sweeps still seed the
shared global occupancy) while sibling repos keep dispatching. The epic supervisor
likewise attaches each cached per-repo supervisor to that root's own
`MainHealthState`. `DaemonStatusReport.main_health_gate_halted` keeps its pre-#3930
meaning (the daemon's own primary workspace); per-repo halt lives in
`per_repo[].health_gate_halted`.

**Empty-registry equivalence**: with a single workspace exactly one root is ever
keyed, so both the status view and the gate cadence/halt semantics reduce to the
pre-#3930 single-workspace behavior byte-for-byte. Enablement is still opt-in
(`LOOM_MAIN_HEALTH_GATE` / `autonomous.mainHealthGate`, precedence env > config >
default); the startup master switch reads the daemon's own workspace config, so
enabling the gate for a genuine multi-repo deployment is done with the machine-
global `LOOM_MAIN_HEALTH_GATE=1` env var (each repo's own `buildGate` block then
decides whether it actually gates).

## Gate verdicts: VERIFIED_RED vs UNEVALUATED (#3974)

A safety gate that fails closed on **its own** infrastructure failures converts
every environmental hiccup into a total dispatch outage — and, for the repo that
contains the gate's own source, into a bootstrap deadlock where the daemon cannot
dispatch the fix for the thing that is broken. Before #3974 *any* non-zero gate
exit (including a timeout, `sh` exit 127 because `cargo` was not on the daemon's
`PATH`, or a spawn failure) was recorded as "main is RED — HALTING".

Every gate run now resolves to exactly one of three outcomes
(`main_health_gate::GateOutcome`):

| Outcome | Meaning | Effect on dispatch |
|---------|---------|--------------------|
| `Green` | command ran to completion, exit 0 | clears any halt |
| `Red` (**VERIFIED_RED**) | command **ran to completion** and reported failure | **halts** this repo's dispatch |
| `Unevaluated` (**UNEVALUATED**) | the gate produced no verdict | **preserves** the previous verdict; logs loudly with the class |

`UnevaluatedClass` names why, and is surfaced in the daemon log and in
`loom-daemon status`:

| Class | Trigger |
|-------|---------|
| `dirty-tree` | non-ignorable local changes; the workspace was never synced |
| `not-on-main` | workspace is on another branch / detached HEAD |
| `local-ahead` | local `main` carries commits `origin/main` lacks (#3912) |
| `git-failure` | a `git` step failed (`rev-parse` / `status` / `fetch` / `rev-list` / `reset`) |
| `timeout` | the gate command exceeded `buildGate.timeoutSeconds` |
| `command-not-executable` | `sh` exit 127 (not found) or 126 (not executable) |
| `killed-by-signal` | terminated by a signal (e.g. an OOM kill) |
| `spawn-failure` | could not spawn the command, or an I/O error while capturing output |
| `contradicted-by-forge-ci` | ran and failed locally, but forge CI is green on the same commit (below) |

The discriminator is deliberately narrow so a genuinely failing build still halts:
**any other non-zero exit is trusted as VERIFIED_RED** — `cargo test`'s 101 for a
failing test still halts dispatch.

### Dirty-tree ignore classes (#3950, #4332)

`dirty-tree` only fires on **non-ignorable** local changes — `is_ignorable_dirt`
(`loom-daemon/src/main_health_gate.rs`) excludes several known-safe classes before
deciding the workspace needs a `Skip`:

| Class | What it covers |
|-------|-----------------|
| Loom-owned transient paths (#3778/#3950) | `.loom/sweep-checkpoint/`, `.loom/tokens/`, `.loom/logs/`, `.loom/worktrees/`, … — the daemon's own runtime bookkeeping |
| Regenerable lockfiles (#3950) | `package-lock.json`, `Cargo.lock`, `pnpm-lock.yaml`, `yarn.lock`, `uv.lock` (matched by basename anywhere in the tree) |
| Re-stamped install manifest (#4239, #4332) | `.loom/install-metadata.json` exactly — generated + re-stamped by every `resync-installed.sh` run, no `defaults/` source to byte-match against |
| Installed-surface byte-match (#4332) | `.loom/hooks/`, `.loom/scripts/`, `.loom/roles/`, `.loom/docs/`, `.loom/bin/`, `.claude/commands/loom/` — ignorable **iff** the dirty file's content byte-matches its tracked `defaults/` counterpart in the same worktree (loom-repo-scoped; a consumer repo has no local `defaults/` tree, so this class never applies there) |

The installed-surface class exists because this repo dogfoods its own install:
`resync-installed.sh` (`.loom/docs/troubleshooting.md` → *Overnight / long-running
orchestration*) refreshes the tracked `.loom/…` / `.claude/commands/loom/` copies
from `defaults/…` whenever they drift, and that refresh itself leaves the tree
dirty until committed. Without this class, every resync produced tracked-file dirt
the gate could not distinguish from an operator hand-edit, so it skipped every
cycle as `dirty-tree` — a permanently blinded gate, not a one-off. Byte-matching
against `defaults/` is the safety property: an operator hand-edit to an installed
copy cannot byte-match content it was never copied from, so it still reports
`dirty-tree` as before; only provable resync output is ignored. A `defaults/…`
SOURCE-side edit is never in this class's domain either way (only the *installed*
copy maps to its source, not the reverse), so editing `defaults/docs/x.md`
directly and then resyncing still correctly skips the gate on that edit.

`resync-installed.sh` also prints the exact `git add … && git commit` command in
its summary when a run leaves the tree dirty with nothing but this kind of resync
output — worth running so the dirt doesn't linger indefinitely (ignorable ≠
committed; the gate proceeds either way, but an uncommitted resync is still a
correctness gap in the repo's history).

`defaults/scripts/check-main-clean.sh` (the sweep-lifecycle backstop for builder
contamination on `main`, #2802/#3513) deliberately does **not** adopt this
byte-match class — see the divergence note in its header and in
`INSTALLED_SURFACE_PREFIXES`'s doc comment. That script protects a different
property (a builder wrote into the main worktree by mistake) where a byte-match
could mask a real, if rare, contamination bug.

### Forge-CI corroboration of a local red

A local run can also fail *because of the host it runs on*: on the incident host
six `integration_basic` tests assert `tmux_session_exists(...)` and fail because
the tmux server is dead, while `.github/workflows/ci.yml` runs the identical
`cargo test --workspace` on the same commit and passes. The local gate measures
**this host**; forge CI measures **the commit**.

So a completed-and-failed run is cross-checked against the forge's CI conclusion
for the exact `origin/main` SHA the gate evaluated (post-sync `HEAD`), via
`gh run list --branch main --json headSha,status,conclusion,workflowName`
matched on `headSha`:

- CI **green** on that SHA → downgraded to `contradicted-by-forge-ci`
  (UNEVALUATED); logged loudly, **does not halt**.
- CI **red** on that SHA → corroborated; still VERIFIED_RED, still halts.
- CI **unknown** → fail safe: still VERIFIED_RED, still halts.

Only *positive* contrary evidence ever relaxes a halt, so "green" is the hardest
verdict to reach and everything short of an unambiguous all-clear degrades to
**unknown**:

| Runs for the evaluated SHA | Verdict |
|---|---|
| any `failure` / `timed_out` / `startup_failure` | red |
| any run not yet `completed` (`queued`, `in_progress`, …) | unknown — CI has not judged the commit yet |
| any `cancelled` / `action_required` / `stale` / unrecognized conclusion | unknown — the workflow reached no verdict about the code |
| at least one `success`, every other run `skipped` / `neutral` | **green** |
| no run for the SHA, unparseable output, `gh` missing/unauthenticated, probe timeout > 30s | unknown |

**Absence of failure is not success.** Requiring at least one genuine `success`
*and* no outstanding or indeterminate sibling run closes two ways a
"saw any completed run ⇒ green" reducer reads green on non-evidence: a
`cancel-in-progress: true` concurrency group leaves superseded runs at
`completed/cancelled` **forever** (a permanent false green for that commit), and a
fast bookkeeping workflow that finishes minutes before the real build would
otherwise vouch for the commit on its own. Neither case needs the probe to know
*which* workflow "counts", so no workflow name is hard-coded.

#### Optional named verification workflow (`LOOM_GATE_CI_WORKFLOW`, #3987)

The unanimity rule above reasons only over *runs that exist*. A workflow a
`paths` / `paths-ignore` filter excluded from a commit produces **no run at all**
(not a `skipped` run), so it is invisible to the reducer. In a **consumer repo**
whose real verification workflow is `paths`-filtered off a commit while only a
fast bookkeeping workflow (line counter, labeler) runs and succeeds, every run is
`completed`/`success` and the reducer above returns `green` for a commit the real
build never judged — relaxing a local red on non-evidence. (Not reachable in this
repo: `.github/workflows/ci.yml` has no `paths` filter, so a `CI` run exists for
every `main` commit.)

The optional `ciWorkflow` knob closes that gap by naming the workflow that
*counts*. It is **unset by default** — absent, behavior is byte-for-byte the
unanimity rule above, so no repo silently changes. Resolution mirrors
`LOOM_MAIN_HEALTH_GATE`, precedence **env > config > default(None)**:

1. `LOOM_GATE_CI_WORKFLOW` env var (empty / whitespace-only ⇒ falls through), or
2. `.loom/config.json` → `autonomous.mainHealthGate.ciWorkflow` (string; empty /
   whitespace ⇒ unset), or
3. absent ⇒ the unanimity rule, unchanged.

When a name **is** configured it layers **one additional requirement on top of**
— never a relaxation of — the unanimity rule: the named workflow must itself have
concluded `success` for the SHA. Matching is **exact and case-sensitive** on
`workflowName`.

| `ciWorkflow` set, runs for the evaluated SHA | Verdict |
|---|---|
| named workflow `completed`/`success`, unanimity otherwise satisfied | **green** |
| named workflow has **no run at all** | unknown — *the gap closure* |
| named workflow `skipped` / `neutral` | unknown (a required workflow that declined did not verify the commit — differs from the unnamed case, where `skipped` does not block green) |
| any run `failure` / `timed_out` / `startup_failure` | red (unchanged, still checked first) |

**Misconfiguration guardrail.** A typo'd name would otherwise pin the gate to
permanent `unknown` (silently recreating #3974 for that repo). When a configured
name appears for **no** SHA anywhere in the probe window, the probe emits a
`log::warn!` naming the configured value and the workflow names actually
observed, then still returns `unknown` (fail safe) — so the cause is visible in
`~/.loom/daemon.log`.

Corroboration is on by default and can be disabled with
`LOOM_GATE_CI_CORROBORATION=0` (for repos with no forge CI or no `gh`); it is only
probed on a local red, never on a green run.

#### Stale-credential gate hold (#5630)

The corroboration rules above reason about **one repo's** commit. They cannot
explain an `N`-of-`N` flip: on 2026-08-07 a saturated fleet host logged
`credential_preflight: github-app refresh tick failed (… timed out after 20s)`
and, minutes later, `work_finder: main-health gate halted dispatch for 22 of 22
repo(s)` — then cleared again on the next successful refresh, all day. Twenty-two
mains did not break and heal in lockstep; the daemon's own forge credential went
stale, so every `gh`/`git fetch` it made started failing **everywhere at once**,
and the gate had no way to tell that apart from twenty-two red mains.

Two changes close it:

1. **The refresh tick is harder to fail.** Its subprocess bound is now
   `forge.githubApp.mintTimeoutSeconds` (default `90`s, up from a fixed
   `20`s) and a transport-level failure — a timeout or spawn error, i.e. the
   helper never produced a parseable answer — is retried **once** before the
   tick is declared failed. A parsed `{"status":"error"}` envelope (bad key,
   app not installed) is deterministic and is **not** retried.
2. **A credential failure is UNEVALUATED, not red.** The daemon tracks each
   refresh source's current consecutive-failure streak — the primary tick
   (#4430) and every cross-owner tick (#5401) are keyed separately, so a healthy
   owner's success cannot clear a failing owner's streak. While **any** streak
   is inside its grace window (`LOOM_FORGE_CREDENTIAL_STALE_GRACE_SECS`, default
   30 min, measured from that streak's **first** failure so it cannot renew
   itself indefinitely):

   - `run_gate_tick` **skips** the tick entirely for every root — the expensive
     command never runs, the tick returns "nothing to apply", and each repo's
     `halted` flag, SHA memo, and backoff are left exactly as they were. A
     green repo stays green; an already-halted repo stays halted.
   - If a run is already in flight when the credential goes stale, a local red
     degrades to `UnevaluatedClass::ForgeCredentialStale` rather than
     VERIFIED_RED — checked **before** the forge-CI corroboration probe, since
     that probe is itself a `gh` call and would fail the same way.

   The status/log surface names the class (`forge-credential-stale`), so an
   operator sees *why* the gate went quiet instead of watching `dispatch_halted`
   oscillate with refresh-tick timing.

The hold is deliberately **bounded**. Past the grace window the pre-#5630
fail-safe reasserts and the gate evaluates normally again: a credential that has
been broken for half an hour is a real operator problem, and freezing every
repo's verdict forever would be a worse failure than the one this fixes. Setting
`LOOM_FORGE_CREDENTIAL_STALE_GRACE_SECS` to a very small value effectively
restores pre-#5630 behavior.

### One shared halt state

`work_finder::tick_multi` derives `TickReport.halted` **directly** from the
`WorkspaceHealthStates` flags the gate writes, rather than accumulating it inside
the candidate-gathering loop. Previously a `list_ready_issues` error short-circuited
the loop *before* its halt check, so a repo whose forge query failed reported
`halted = false` — the incident's `work_finder: main-health gate cleared —
resuming dispatch` logged in the same window the gate logged `still RED`. The two
loops now read one source of truth and cannot disagree.

`loom-daemon status` reports the not-evaluated **cause** verbatim
(`main_health_gate.not_evaluated_reason`, `per_repo[].health_gate_not_evaluated_reason`)
instead of the pre-#3974 hard-coded "workspace tree is dirty", which misreported
timeouts, missing tools, and `git fetch` failures on a clean tree as a dirty tree.

### SHA memoization, `realChangeGlobs`, and indeterminate-run backoff (#3984)

`buildGate.realChangeGlobs` was declared in shipped config but never read
anywhere in `loom-daemon/src/` — the gate re-ran its (potentially
minutes-long) `buildGate.command` on **every** cadence tick regardless of
whether `origin/main` had moved at all. On a contended host that full
compile+test run could not finish inside `buildGate.timeoutSeconds`, the
timeout was correctly classified UNEVALUATED (#3974) and left the previous
halt verdict standing — but the very next tick fired again almost
immediately (the gate's own cadence is far shorter than a realistic build
timeout), so the run competed with itself and with in-flight sweeps for
cores in a self-sustaining doom loop, permanently HALTING dispatch for that
repo.

`spawn_multi_main_health_gate_task` now calls `run_gate_tick` per root per
tick instead of unconditionally constructing a `CommandGateRunner`:

1. **Backoff check** (`MainHealthState::gate_backoff_active`) — if the
   previous tick was UNEVALUATED, the tick is skipped outright while a
   backoff window is active. `record_gate_indeterminate_backoff` grows that
   window exponentially — `buildGate.timeoutSeconds * 2^(consecutive - 1)`,
   capped at `MAX_GATE_INDETERMINATE_BACKOFF` (1 hour) — so a gate stuck on
   a broken `PATH`/dead process tree/contended host waits at least a full
   timeout period (then longer) before retrying, rather than restarting
   immediately. Any determinate (Green/Red) evaluation clears the backoff.
2. **SHA memoization** — a cheap `git ls-remote origin main` (no fetch, no
   working-tree mutation) resolves the current `origin/main` SHA. If it
   equals `MainHealthState::gate_last_evaluated_sha` (the SHA of the last
   determinate Green/Red evaluation, or of a SHA previously reviewed and
   found to touch no `realChangeGlobs` path) the tick is skipped
   unconditionally — an unchanged SHA has no diff at all, so no glob could
   possibly match. This is the common case in the reported incident (`main`
   sat at one SHA with all forge CI green for hours) and is what actually
   ends the doom loop.
3. **`realChangeGlobs` filtering** (`decide_gate_run` /
   `diff_touches_globs`) — when the SHA *has* moved and `realChangeGlobs` is
   non-empty, `git diff --name-only <last>..<current>` is checked against
   the configured glob patterns (`glob_matches`: `*`/`?` wildcards; a
   pattern with no `/` matches by basename anywhere in the tree, e.g. `*.rs`
   matches `loom-daemon/src/main.rs`; a pattern containing `/` matches the
   full repo-relative path). A diff touching no matching path skips the run
   (the previous verdict stands) and still advances the SHA memo, so a
   docs-only merge doesn't retrigger a diff computation on every subsequent
   tick either. An empty `realChangeGlobs` (the default) means *any*
   movement of `main` counts as real — unchanged from pre-#3984 behavior
   once the SHA has actually changed. A diff that cannot be computed (e.g. a
   missing object) fails safe and runs the gate rather than risking a real
   change hiding behind an uncomputable diff.

None of this changes the VERIFIED_RED / UNEVALUATED discriminator (#3974) or
the forge-CI corroboration (above) — it only decides whether the (expensive)
command needs to run again at all before those checks ever see it.

## Per-workspace priority tiers (#3946)

By default the multi-repo work-finder and epic supervisor iterate
`effective_roots()` in **registration order**, so a deep product-repo backlog can
starve the tool repos whose fixes compound. Priority tiers add cross-repo dispatch
ordering:

- **Registry schema** — each `~/.loom/workspaces.json` entry gains an optional
  `priority` integer (`Workspace.priority`, `loom-daemon/src/workspace_registry.rs`):
  **lower = higher priority**, default `100`
  (`DEFAULT_WORKSPACE_PRIORITY`). Fully backward compatible — an entry with no
  `priority` (every pre-#3946 file) deserializes as `100` via `#[serde(default)]`,
  so an all-default registry orders exactly as before.

- **CLI** —
  - `loom-daemon workspace add <path> --priority N` registers a repo in tier `N`
    (default `100`).
  - `loom-daemon workspace set-priority <path> N` retiers an already-registered
    repo.
  - `loom-daemon workspace list` prints a `PRIO` column, sorted highest-priority
    first (mutation order on disk is preserved).

- **Work-finder ordering** — `work_finder::tick_multi` now takes a
  `priorities: &[u32]` slice parallel to the workspaces. Instead of dispatching
  each repo's backlog in registration order, it gathers **every** eligible
  candidate across all workspaces into one queue, sorts it by `candidate_cmp` —
  **(workspace priority asc, `loom:urgent` first, issue age asc/oldest-first,
  issue number asc)** — and fills the single shared concurrency budget in that
  global order. The cap/budget mechanics (#3811/#3930) are unchanged; this only
  orders the queue. `createdAt` is added to the `gh issue list --json` fields for
  the age key.

- **Epic supervisor** — `spawn_multi_supervisor_thread` reorders its cached
  per-repo supervisors by workspace priority each tick (stable within a tier) before
  `tick_multi`, so higher-tier epics advance/dispatch first.

- **Status** — `RepoStatus.priority` is surfaced in the `loom-daemon status`
  **Managed repos** table (a `PRIO` column) and the `--json` `per_repo[].priority`
  field, with the breakdown sorted highest-priority first.

**Starvation stance (v1):** strict priority is intentional — tool repos are small
queues that drain fast. A permanently-full higher tier **will** starve lower tiers;
fairness knobs (per-tier slot reservations) and cross-repo dependency awareness are
explicit follow-ups, deferred until observed to matter.

## Forge-side pipeline snapshot (`status --pipeline`, #3977)

`loom-daemon status` shows the *dispatch*-side picture (in-flight sweeps, the
dynamic cap, token health, per-repo priority/gate state — see the two sections
above), but none of that answers "how is the work actually progressing?" —
that requires forge queries the daemon's IPC handler deliberately does not
make (the `DaemonStatus` round-trip stays a fast, network-free read). The
`--pipeline` flag adds a client-side, opt-in forge-side pipeline snapshot per
managed repo, fetched *after* the IPC round-trip completes:

- **Counts** — for each root in `report.per_repo` (already priority-ordered):
  open, dispatchable `loom:issue` rows (queued — `gh issue list --search
  "is:open label:loom:issue -label:loom:blocked -label:loom:operator-only"`,
  excluding every `work_finder::PARK_LABELS` park label so `queued` matches
  the work-finder's own admission definition, #4825 — `PARK_LABELS` is
  `loom:blocked` / `loom:operator-only` only as of #5817; the Rust daemon's own
  dispatch is a separate follow-up from `/loom:sweep`'s skip parity, see
  "Park-label dispatch guard" below), open `loom:building`
  (claimed), open PRs by `loom:review-requested` / `loom:changes-requested` /
  `loom:pr`, and PRs merged in the last 24h (`gh pr list --state merged
  --search "merged:>=<24h-ago RFC3339>"`).
- **Module** — `loom_daemon::pipeline_snapshot`: `PipelineSource` is the forge
  abstraction (mirrors `work_finder::WorkSource` / `GhWorkSource`),
  `GhPipelineSource` is the `gh`-backed implementation (six `gh` invocations
  per repo, `current_dir(root)` so `gh` auto-detects that repo's own remote —
  same convention as `GhWorkSource::for_root`), and
  `collect_pipeline_snapshots` fans the per-repo fetch out onto Tokio's
  blocking-thread pool so N managed repos cost roughly one repo's worth of
  wall-clock latency.
- **Resilience** — every count is fetched and allowed to fail independently;
  a failed metric renders as `?` (`RepoPipelineSnapshot::error` names the
  first failure) without blocking the other metrics for that repo or any
  sibling repo's snapshot — the same per-workspace error-isolation rule the
  work-finder's `tick_multi` already applies to dispatch.
- **Output** — `loom-daemon status --pipeline` adds a "Forge pipeline" table
  below the existing "Managed repos" table (same row order); `--json
  --pipeline` adds a `pipeline` array (`null` when `--pipeline` was not
  passed, so a consumer can distinguish "not requested" from "requested but
  empty"). Terminal-friendly and safe to `watch -n 60`.
- **Why opt-in** — six `gh` calls per managed repo is too slow to bundle into
  the default view (which is used for frequent, low-latency operator checks);
  `--pipeline` trades that latency for the queue-depth picture on demand.
- **Metric mask (#4761)** — `GhPipelineSource::with_metrics(PipelineMetrics)`
  selects which of the six counts to fetch (each is one `gh` call, run
  sequentially within a repo). `PipelineMetrics::ALL` is the default and what
  `status --pipeline` / the dashboard use; `PipelineMetrics::HEALTH` (queued +
  merged) is what `loom-daemon health` uses to stay inside its latency budget.
  `with_merge_window(Duration)` widens/narrows the merge-throughput window from
  its 24h default. A masked-off metric is left `None`; a caller that masks a
  metric off simply must not read it.

## One-shot fleet vitals (`loom-daemon health`, #4761)

```
loom-daemon health [--since 30m] [--json]
```

One consolidated, structured vitals report with an **exit-code contract** so a
watch loop (human, cron, dashboard, or the `loom:watch` skill) can branch
without parsing anything:

| exit | meaning |
|------|---------|
| `0` | every section green |
| `1` | degraded — at least one section is non-green for a reason other than the busy-timeout case below, **including "could not determine"** |
| `2` | the daemon is genuinely dead |
| `3` | busy, not confirmed unhealthy — see "Busy vs degraded" (#6191) below |

Seven sections, one line each (or the full structured payload with `--json`):

| section | what it reports | source |
|---------|-----------------|--------|
| `liveness` | trusted liveness verdict | `daemon_install_state::probe()` + `pgrep_daemon_pids()` + the IPC round-trip |
| `dispatch` | in-flight vs dynamic cap, plus the last work-finder tick's dispatch/skip-reason summary | `DaemonStatusReport` + `work_finder::last_tick_summary()` |
| `tokens` | healthy/total, exhausted count, `.ranking` staleness | `CapacityReport` + the resolved pool's `.ranking` mtime |
| `roles` | **persistent** role-tick failures (transient ones are a count only) | `role_runner::role_tick_records()` |
| `role_liveness` | roles configured to tick that have gone **silent** — no tick at all in `>= 4x` their own interval (#6201) | `role_runner::last_role_tick_snapshot()` + each root's `role_runner_enabled`/`role_runner_roles` |
| `queues` | per-root ready (`loom:issue`) counts **plus the review-side axes** (`loom:review-requested` / `loom:changes-requested` / `loom:pr`), and a per-repo *review stall* verdict | `pipeline_snapshot` (`PipelineMetrics::HEALTH`) |
| `throughput` | merges across managed repos inside the window | `pipeline_snapshot` (`PipelineMetrics::HEALTH`) |

#### `queues`: the review-stall rule (#5021)

`queues` reports a repo **DEGRADED** ("REVIEW STALLED") when it is carrying at
least `health::REVIEW_STALL_MIN_BACKLOG` (3) open `loom:review-requested` PRs
**and** the window merged nothing at all. Neither half alone is a fault — a
short review queue is normal, and a quiet window legitimately merges nothing
(`throughput` treats zero merges as green for exactly that reason) — but the
conjunction is a direct, cause-agnostic observation that review is not
happening. It is evaluated **per repo**, so a repo with zero ready issues
cannot be masked by the fleet-wide `total_ready` sum. An axis that was not
observed (forge query failed, or the caller masked the metric off) is reported
as `null`/`?`, never as `0`, and never produces a stall verdict in either
direction.

### Liveness precedence: pgrep + pid-file first, launchd NEVER alone

This is the load-bearing rule. #4694's launchd domain probe twice declared a
live, *dispatching* daemon dead during an overnight watch; the daemon's
singleton guard was the only thing that prevented a sweep-killing restart. So
`health` declares DEAD only on **positive evidence of absence**, in this order:

1. **IPC answered** ⇒ alive, unconditionally. No local probe overrules a daemon
   that just answered a round-trip.
2. **`daemon_install_state` says the process is alive** — that classification is
   itself launchd probe → skipped-domain cross-check → pid-file cross-check, so
   it already refuses to trust a lone launchd negative ⇒ DEGRADED
   (alive-but-unresponsive / still-starting), never DEAD.
3. **`pgrep -x loom-daemon` finds a live process** ⇒ still DEGRADED. The third
   independent signal, for a daemon started outside the managed wrapper or one
   whose pid file was removed by hand.
4. Only when **all three** are negative is the verdict DEAD (exit `2`).

An *undiagnosable* probe (no loom dir resolvable, `pgrep` absent) is `UNKNOWN`
(exit `1`) — "I could not tell" — never exit `2`.

### Busy vs degraded: the probe-budget-exhausted case (#6103, #6191)

`health` bounds its IPC round-trip far tighter than `status`'s load-scaled
5-30s budget or the watchdog's 15s-per-tick / 3-consecutive-failure budget —
deliberately, so a wedged daemon is reported fast. On a busy host (multiple
sweeps in flight, high load average) that tight budget alone used to
manufacture a false alarm: a round-trip that simply did not complete in time
against a daemon the watchdog's own consecutive ticks confirmed was healthy
still flipped `overall` to `DEGRADED`/exit `1` — indistinguishable, at the
exit-code level, from a genuine fault.

Two collected-without-IPC signals `health` already has — the process is alive
(`daemon_install_state::probe()`) and its heartbeat is fresh
(`InstallStateReport::heartbeat_freshness`) — are used to tell "busy" apart
from "degraded" in two stages:

1. **Retry escalation.** A first IPC attempt that merely times out (never a
   harder failure) is retried once. If local evidence already corroborates
   the process as alive with a fresh heartbeat
   (`health::alive_with_fresh_heartbeat`), the retry uses an escalated ~10s
   budget instead of repeating the same short one — worth waiting a little
   longer for, rather than giving up at the same budget twice. Without that
   corroboration (heartbeat stale, unreadable, or the process undiagnosable),
   the retry uses the same budget as the first attempt, unchanged from #6103.
2. **A distinct overall verdict and exit code.** If the escalated retry also
   times out, and *every* non-green section traces back to that same
   exhausted probe budget against a daemon already corroborated as alive with
   a fresh heartbeat, `overall` is reported as `"indeterminate-busy"` (not the
   ordinary `"unknown"`) at its own exit code, **`3`** — distinct from both
   `EXIT_HEALTHY` (0) and `EXIT_DEGRADED` (1). A watch loop (including the
   `/loom:watch` skill) can treat exit `3` as "try again shortly" rather than
   an alert, without re-implementing the watchdog's own consecutive-failure
   streak logic by parsing `--json`.

A timeout that lacks either corroborating signal — a stale/unreadable
heartbeat, an undiagnosable liveness probe, or a hard (non-timeout) IPC
failure — gets no benefit of the doubt: `overall` stays the ordinary
`"unknown"`/exit `1`, exactly as it did before #6191. This distinction is
deliberately narrow: an `UNKNOWN` section caused by something else entirely
(a missing `gh` binary on this caller's `PATH`, for instance) is unaffected
and still contributes to the ordinary exit `1`.

### Transient vs persistent role ticks

The role-runner loop appends every `(root, role)` tick outcome to a bounded
process-global ring (`role_runner::ROLE_TICK_RING_CAPACITY`), carried verbatim in
`DaemonStatusReport::role_tick_records`. The *classifier* is client-side
(`health::summarize_role_ticks`) because the window is the operator's choice:
inside `--since`, a `(root, role)` pair whose **latest** record is a failure is
**persistent** (surfaces, DEGRADED); one that failed but whose latest record is a
success is **transient** (reported as a count only). Recording happens *before*
the log-dedup decision, so #4349's DEBUG-downgraded repeat failures are still
fully visible to a health check.

### Role liveness: "is it ticking at all" (#6201)

`roles` (above) classifies the *outcomes* of ticks that DID happen, inside a
bounded, client-chosen window sourced from the shared, capacity-bounded
`role_tick_records` ring (`ROLE_TICK_RING_CAPACITY = 128`, process-global
across every role and every managed workspace). A role that stops ticking
**entirely** while several other roles on the same workspace keep ticking
normally has its ring entries evicted within hours — at which point `roles`
sees zero records for it and reports a clean bill of health instead of a
silent, indefinite gap.

This is *a* false-green, but it is **not** the mechanism behind the incident
that filed #6201 — see "Pre-spawn skips are logged to the role's own log"
below for the confirmed one.

`role_liveness` answers a different, ring-independent question. Every tick
also stamps a **never-evicted** last-tick timestamp per `(root, role)` pair
(`role_runner::last_role_tick_snapshot`, wire field
`DaemonStatusReport::role_last_tick`) — bounded by `(root, role)` cardinality,
not by tick volume. `assess_role_liveness` cross-references that against each
registered root's own `role_runner_enabled` + `role_runner_roles` (what
SHOULD be ticking there) and flags a pair `DEGRADED` once it has gone silent
for `>= 4x` (`health::ROLE_LIVENESS_STALE_MULTIPLIER`) its own
`RoleSpec::default_interval_secs`. A pair that has **never** ticked at all is
deliberately not flagged (it may simply have been enabled moments ago —
there is no daemon-uptime signal to distinguish that from "broken since
before this process started").

### Pre-spawn skips are logged to the role's own log (#6201)

**The tick loop never benches a role.** `spawn_multi_role_task`'s interval loop
retries every registered root on every interval regardless of the previous
tick's `RoleTickOutcome` — there is no failure-count state, no backoff, and no
"disabled after N failures" anywhere in `role_runner.rs`. That is pinned by
`failure_outcome_is_retried_on_the_very_next_tick_never_benched` and
`role_recovers_automatically_once_the_broken_runtime_works_again`.

What produced #6201's nine-day silence was a **sticky config-shaped skip that
is retried forever and fails identically forever**, invisible on both surfaces
an operator checks:

1. `runtimes.roles.<role>` (here `curator: "codex"`, a leftover runtime
   experiment in a `.loom-local/local.json` tier) admits the role onto a
   runtime its own `roles/<role>.json` `suggestedWorkerType` never asked for.
2. The paired `autonomous.roleRunner.roleModels.<role>` pin that made that
   runtime usable is later removed, so the model falls back to the
   Claude-shaped built-in default (`sonnet`) while the admitted runtime stays
   `codex`.
3. Every subsequent tick trips the #5028 model/runtime mismatch preflight and
   returns `RoleTickOutcome::ModelRuntimeMismatch` — **before**
   `run_role_with_timeout`, the only writer of `.loom/logs/role-<role>.log`,
   is ever reached. The role's own log therefore stops at the last real spawn
   and never grows again.
4. In `daemon.log`, the mismatch WARN is deduped to the state *edge*
   (`RootTickLogAction::ModelMismatchRepeat` downgrades repeats to `DEBUG`),
   so a persistent condition yields exactly one WARN per daemon process.

`role_liveness` does not catch this shape either: the tick *is* recorded, so
the last-tick timestamp stays fresh while the role does nothing. The fix is to
make the artifact operators actually read honest — all four pre-spawn bail-outs
(unresolvable spawn bin, `NoTokenPool`, `RuntimeRejected`,
`ModelRuntimeMismatch`) now append a dated line to `role-<role>.log`:

```
==== loom-daemon role_runner: 2026-08-14T16:45:51+00:00 role=curator SKIPPED BEFORE SPAWN (#6201): model/runtime mismatch: runtime "codex" only accepts Codex models, but the resolved model "sonnet" is a Claude model (model source=default); set autonomous.roleRunner.roleModels.curator to a model the codex runtime accepts, or point this role back at a Claude runtime ====
```

A role stuck this way now shows a growing, timestamped, self-diagnosing trail
in the file whose staleness was the original symptom.

### One collector, three consumers

`health::assess(&HealthInputs) -> HealthReport` is pure — no daemon, no forge, no
subprocess — and is the *only* place any verdict rule lives:

- `loom-daemon health` (`cli::health`) collects the inputs and exits with
  `HealthReport::exit_code()`.
- The dashboard's `GET /api/health` (`serve.rs`) calls the same `assess` over the
  same cached `/api/pipeline` `gh` batch. An unreachable daemon there answers
  **200 with `overall: "dead"` / `exit_code: 2`**, not 503 — "the daemon is dead"
  is a health *report*, not a transport failure.
- `loom-daemon status` keeps its own (unchanged) rendering; `fleet status`
  reuse is a follow-up (it would need a `health --json` fan-out over ssh).

## Reaper task

The reaper (`sweep_registry::spawn_reaper_task`) ticks every 30 seconds
(env-overridable via `LOOM_SWEEP_REAPER_INTERVAL_SECS`). Each tick:

1. Snapshots live `Running`/`Pending` entries.
2. Tests each PID via `kill(pid, 0)`.
3. On dead PID:
   - If a sweep checkpoint exists at
     `.loom/sweep-checkpoint/issue-<N>.json`, marks the entry `Crashed`
     and flips the forge label `loom:building` → `loom:issue` so the
     next dispatch resumes from the checkpointed phase.
   - Otherwise marks the entry `Exited{code: None}`.
   - Emits `sweep.issue.{N}.exited` or `sweep.issue.{N}.crashed`, plus
     a global `sweep.global.completed` event.
4. Garbage-collects terminal entries older than the retention window
   (default 1 hour).

## Stale-claim reconciliation & the sweep journal (#3953, fixed #3975, extended to PR-side claims #4367)

Two independent surfaces reclaim abandoned `loom:building` claims when the
sweep that owned them has died, using the same evidence source and the same
decision rule:

| Surface | Where | When it runs |
|---------|-------|---------------|
| Rust issue-side reconciliation (`loom:building`) | `claim_reconciliation::forge::reconcile_workspace` (guarded by `LOOM_STALE_CLAIM_RECONCILE`, default on) | At daemon startup AND every `LOOM_CLAIM_RECONCILE_INTERVAL_SECS` (default 600s) thereafter, via `run_reconciliation_pass` (#4348), across every `effective_roots()` workspace |
| Rust PR-side reconciliation (`loom:reviewing` / `loom:treating`, #4367) | `claim_reconciliation::forge::reconcile_pr_claims`, called from the same `run_reconciliation_pass` entry point (same `LOOM_STALE_CLAIM_RECONCILE` gate — no separate wiring) | Same cadence as the issue-side pass: at startup and every `LOOM_CLAIM_RECONCILE_INTERVAL_SECS`, across every `effective_roots()` workspace |
| `loom-recover-orphans` (native, issue #4272) | `worktree_ops::orphan_recovery::check_untracked_building` (issue-side) + `check_stale_pr_claims` (PR-side, issue #6167 — delegates to `claim_reconciliation::forge::reconcile_pr_claims_report`, the same `plan_pr`/`decide_pr` logic the Rust PR-side row above runs) | On demand (operator/cron invocation of `loom-recover-orphans [--recover]`) |

Both read the same machine-level **sweep journal** (`~/.loom/sweeps.json`,
override `LOOM_SWEEPS_JOURNAL_PATH`, written by `sweep_journal::record_sweep`
on every `SweepRegistry::dispatch`) and apply the same three-way decision
rule per `loom:building` issue:

1. **Journal entry with a live recorded PID** → keep (genuinely in-flight).
2. **Journal entry with a dead recorded PID** → reclaim. This is the
   strongest possible evidence — a specific PID that provably no longer
   exists — so both surfaces treat it as authoritative.
3. **No journal entry at all** → reclaim only once the claim has been stale
   longer than `LOOM_STALE_BUILDING_HOURS` (default 4h; also drives the
   Rust-side default `DEFAULT_STALE_BUILDING_HOURS`). Absence of a record
   might mean a live manual `/loom:sweep` the journal was never told about
   (a pre-journal claim, or the journal file just doesn't exist on this
   machine), so a much wider grace window applies than for a claim the
   journal has proof about.

### The #3975 bug: pruning before deciding

The journal is deliberately self-pruning — `sweep_journal::upsert` (every new
dispatch) and the Rust reconciliation pass both call `prune_dead` to drop
dead-PID entries so the file never accumulates an unbounded graveyard
(mirrors `sweep-run-registry.sh`'s `prune_dead`/`peers`, #3768).

Before #3975, `reconcile_workspace` called `prune_dead` **before** calling
`plan()`/`decide()` — which deleted the exact dead-PID entry the `DeadPid`
branch above needs to fire its immediate, unconditional reclaim. With the
entry gone, every claim silently fell through to branch 3 (no record) and
its much longer `stale_hours` grace window, even for a sweep that had *just*
died. Two claims in a downstream workspace (issues #6170/#6173: SIGTERMed
during a daemon restart) sat un-reclaimed for hours because of this —
`loom-recover-orphans --recover` found and fixed only a third, unrelated
claim (#6172) whose label happened to already be older than the stale-hours
threshold. The fix: `reconcile_workspace` now decides against the raw,
unpruned journal first; pruning happens only afterward, via the normal
per-reclaimed-issue `remove_sweep` cleanup (leftover dead entries for
untouched issues are still pruned lazily by the next `upsert` elsewhere).

The Python side (`gather_liveness_evidence` / `check_untracked_building`)
never pruned the journal itself — it only reads whatever the file currently
contains — so it was not directly bugged the same way, but it inherited the
same *symptom*: by the time an operator ran `loom-recover-orphans` by hand,
a routine daemon dispatch (or the (now-fixed) Rust startup pass) had often
already pruned the dead-PID evidence out from under it, leaving only the
weaker "no record" signal to work with.

### Never-silent staleness-gate skips (#3975)

Separately, `check_untracked_building`'s staleness gate (branch 3 above, and
the short `label_grace_period` gate that also applies to branch 2) used to
log its "SKIPPED: #N ..." line **only** under `--verbose`, so a default
(non-verbose) `loom-recover-orphans` run gave no visible trace that a claim
had been seen and excluded — indistinguishable from the tool never having
looked at that issue at all. `OrphanRecoveryResult.watched` now records
every staleness-gated skip (issue, reason, label age, threshold) regardless
of `--verbose`, and both `format_result_human` and `--json` output always
include it. A watched entry is explicitly **not** an orphan — it may still
be alive — but it is never silently dropped.

### One intentional asymmetry: dead-PID grace period

Rust's `decide()` reclaims a `DeadPid` claim **unconditionally** — no
staleness check at all (see `decide_dead_pid_overrides_label_age`). Python's
`check_untracked_building` still applies the short `label_grace_period`
(default 600s) even to a `journal_pid_dead` reason. This is intentional, not
a bug to unify: the Rust pass runs once per daemon *start*, a rarer and more
deliberate event, while the Python tool can be invoked ad hoc (including
immediately after a claim is made, before its journal entry has even been
written) — the short grace period is defense-in-depth against a race the
once-per-restart Rust pass is much less exposed to.

### PR-side claim labels: `loom:reviewing` / `loom:treating` (#4367)

`loom:reviewing` (Judge) and `loom:treating` (Doctor) are claim *overlays* —
they coexist with the PR's underlying state label
(`loom:review-requested` / `loom:changes-requested` / `loom:pr`) while work
is in progress. Like `loom:building`, they can be left behind forever when
their holder dies (observed on PR #4303: a `loom:treating` label ~5h stale
from a Doctor killed on another host). `claim_reconciliation::forge::reconcile_pr_claims`
extends the same pass to cover both, running from the same
`run_reconciliation_pass` entry point (startup + periodic, same
`LOOM_STALE_CLAIM_RECONCILE` gate — no `main.rs` change needed).

The pure decision function, `decide_pr`, mirrors `decide`'s union-probe join
priority (journal entry, then checkpoint→run-registry) but with two
deliberate divergences from the issue-side rule, both driven by the PR side's
weaker evidence:

- **The join is heuristic, not authoritative.** A PR has no `loom:building`
  issue number of its own — the issue number is recovered only when the PR's
  `headRefName` matches the `feature/issue-<N>` convention `worktree.sh`
  establishes (`parse_issue_from_branch`). Any other branch shape has no join
  key at all and falls straight through to the age rule below.
- **The age gate applies unconditionally — even to a dead joined pid.**
  Unlike `decide()`'s `DeadPid`/`DeadRunRegistry` branches (immediate,
  unconditional reclaim), a dead pid alone is never sufficient proof on the
  PR side: the branch-name join can be wrong (a Doctor may hold a PR whose
  sweep record belongs to a different phase), so `decide_pr` only reclaims
  once the claim's age has *also* aged past the per-label threshold. A
  **live** joined pid still short-circuits to `Keep` unconditionally — that
  evidence is trustworthy either way.

**Freshness signal: `claim_labeled_at`, not `updatedAt` (#4618).** The age
gate above originally used the PR's aggregate `updatedAt` — but GitHub bumps
that on ANY comment, including a Judge/Doctor "standing down, not stomping"
comment posted by a later pass that *declined* to reclaim. That made the
check self-perpetuating: once a claim survived long enough for one stand-down
comment to post, every subsequent pass saw a "recently updated" PR and never
reclaimed it (PR #4614 — 3 consecutive stand-down comments, never reclaimed).
`decide_pr` now prefers `ClaimedPr::claim_labeled_at` — the timestamp of the
claim label's own most recent `labeled` timeline event, fetched via
`fetch_claim_labeled_at` alongside `list_prs_with_label` — because posting a
comment does not re-apply the label, so a stand-down comment cannot bump it.
`updated_at` is kept only as the fail-open fallback for when the timeline
fetch itself fails or returns nothing (mirroring the pre-#4618,
missing/unparseable-`updatedAt`-fails-safe-to-`Keep` posture). See
`claim_reconciliation.rs`'s `decide_pr` doc comment for the full rationale
and its `decide_pr_reclaims_via_claim_labeled_at_despite_standdown_inflated_updated_at`
regression test, which reproduces the PR #4614 shape directly.

**Liveness means the *claimant's* own heartbeat, on both sides (#4638, narrowed
by #6523).** Anchoring solely on `claim_labeled_at` reclaimed a genuinely live
but non-pid-joinable claimant out from under itself, so `decide_pr` anchors on
`max(claim_labeled_at, most_recent_claim_activity_at)`. #4638 shipped that
second term as "the newest comment since the claim that is not a marker-tagged
stand-down note" — the same conflation the agent side carried as
`COMMENTS_AFTER`, where a routine Builder post-push note or a Champion notice
reads as proof the *claimant* is alive. #6514 fixed that on the agent side
(`defaults/scripts/claim-staleness.sh`); #6523 brings the daemon into line, so
**one definition of liveness now applies to both**:

> A comment counts as claimant activity **iff** it carries that claim's own
> marker — `<!-- loom:claim-activity claim=$CLAIMED_AT -->`, keyed on the
> `labeled` event's timestamp — and is not a stand-down comment.

On the Rust side this is `CLAIM_ACTIVITY_MARKER_PREFIX` / `claim_activity_marker()`
/ `most_recent_claim_activity_at()` in `claim_reconciliation.rs`, documented as
matching `claim-staleness.sh`'s `ACTIVITY_PREFIX` byte-for-byte (the string its
`marker` subcommand prints); `forge::fetch_most_recent_claim_activity_at` only
*narrows* the `gh` query, the predicate itself is the pure, unit-tested
function. Consequences, all shared with the agent side:

- Every other comment — Builder status notes, Champion notices, human chatter,
  bots — neither pins **nor extends** the claim.
- A heartbeat **resets an idle clock** rather than pinning: because the anchor
  is a `max()` of timestamps, one heartbeat buys exactly one more
  `LOOM_STALE_REVIEWING_MINUTES` / `LOOM_STALE_TREATING_MINUTES` window measured
  from the heartbeat's own timestamp.
- A marker left over from an **earlier** claim generation (before a reclaim +
  re-claim) does not refresh the new claim — both sides match on the claim's own
  labeled-at timestamp, not on the marker prefix alone.

This narrowing only ever makes the daemon reclaim *sooner*. The per-label age
floors below are untouched and remain the veto no comment-activity outcome can
bypass — the protection against the #4618 double-claim race that #4790 showed is
load-bearing (`decide_pr_age_floor_vetoes_reclaim_regardless_of_comment_activity`
covers it, including for a dead joined pid). A **live** joined pid still
short-circuits to `Keep` ahead of the age gate entirely.

Thresholds are minutes-scale, not hours, and env-overridable:

| Claim label | Env var | Default |
|-------------|---------|---------|
| `loom:reviewing` | `LOOM_STALE_REVIEWING_MINUTES` | 30 — the SAME env var `.claude/commands/loom/judge.md`'s "Stale `loom:reviewing` Claim Check" already established, so the agent-side fast path and this always-on daemon backstop share one convention |
| `loom:treating` | `LOOM_STALE_TREATING_MINUTES` | 60 — a Doctor's fix cycle legitimately runs longer than a single Judge review pass; the SAME env var `.claude/commands/loom/doctor.md`'s "Stale `loom:treating` Claim Check" fast path uses (#4527) |

Reclaiming removes only the stale claim label — the PR's state label (still
present in the normal case) restores discoverability by itself. As a safety
net, if the PR is left carrying none of `loom:review-requested` /
`loom:changes-requested` / `loom:pr` after the claim label is removed,
`forge::reclaim_pr` adds `loom:review-requested` so a fresh Judge pass picks
it back up. Log lines mirror the issue-side format, e.g. `claim_reconciliation:
removed stale loom:reviewing from PR #N in <root> (DeadPid { pid: … }, …)`.

**Agent-side fast paths are complementary, not redundant (#4527).** This pass
is periodic (up to `LOOM_CLAIM_RECONCILE_INTERVAL_SECS`, default 600s, of lag)
and only ever sees *finished* state, so both role prompts carry a synchronous
check that fires the moment another agent revisits the same PR: judge.md's
"Stale `loom:reviewing` Claim Check" and doctor.md's "Stale `loom:treating`
Claim Check", sharing these exact env vars and defaults. doctor.md additionally
carries a **pre-push head-SHA recheck** (capture `headRefOid` at claim time,
re-compare before the final push) — the one race this pass structurally cannot
cover, since it never hooks into an in-flight Doctor's push.

**Agent-side stand-down marker convention + bounded fallback (#4618, age-floor
join added by #4798).** The same self-perpetuating-comment defect fixed on
the daemon side above also existed in judge.md's and doctor.md's own
`COMMENTS_AFTER` heuristic — the fast path shared the daemon backstop's blind
spot exactly, since both read "any comment after the claim" as fresh. Both
role prompts now (1) tag every stand-down comment they post with `<!--
loom:standdown claim=$CLAIMED_AT -->` and exclude marker-tagged comments from
`COMMENTS_AFTER`, and (2) count marker-tagged comments in a separate
`STANDDOWN_COUNT`, force-reclaiming once `STANDDOWN_COUNT >=
LOOM_MAX_STANDDOWN_STREAK` (default **3**) **AND** the claim's own age is ≥
`LOOM_STALE_REVIEWING_MINUTES`/`LOOM_STALE_TREATING_MINUTES` — regardless of
`COMMENTS_AFTER` — a bounded fallback that holds even if the marker-exclusion
logic above is somehow bypassed. The age floor (#4798) was added after PR
#4790 showed the streak count alone conflates **peer arrival rate** (several
concurrent Judges/Doctors each standing down within minutes of each other)
with **claim liveness**: a claim only 17m36s old — well under the 30-minute
default — was force-reclaimed purely because 3 Judges happened to revisit it
in quick succession, not because the claim itself had gone stale. Joining the
streak condition with the same age floor the ordinary staleness row already
uses vetoes that false positive while still catching the original #4614
livelock (a claim genuinely idle for 30+ minutes). `LOOM_MAX_STANDDOWN_STREAK`
is shared by both role prompts, mirroring how
`LOOM_STALE_REVIEWING_MINUTES`/`LOOM_STALE_TREATING_MINUTES` are already
shared.

**Superseded on the agent side by `claim-staleness.sh` (#6514).** The
`COMMENTS_AFTER` heuristic described above turned out to livelock in the
opposite direction: because it credited *any* comment after the claim as
claimant liveness, one routine Builder post-push status note pinned a claim
"fresh" for the rest of its life, while #5123's duplicate-stand-down
suppression simultaneously froze `STANDDOWN_COUNT` at 1 so the bounded fallback
could never fire either (PR #6513). judge.md, doctor.md and curator.md now all
drive one shared, unit-tested evaluator — `.loom/scripts/claim-staleness.sh`
(source: `defaults/scripts/claim-staleness.sh`) — which (1) counts only
comments carrying `<!-- loom:claim-activity claim=$CLAIMED_AT -->` as claimant
activity, and treats that activity as *resetting an idle clock* rather than
pinning the claim, and (2) bumps a `seq=` counter inside the single
stand-down comment's marker instead of skipping the pass, so the streak keeps
accumulating toward `LOOM_MAX_STANDDOWN_STREAK` with no duplicate comments. The
env vars, defaults and the #4798 age floor are unchanged, and the bounded
fallback stays keyed on the **claim's** age (not the idle clock) so an activity
marker emitted in a loop cannot hold a claim indefinitely. The daemon-side pass
below is unaffected — it never consulted comments in the first place.

### `loom:curating` (Curator): agent-side only, no daemon backstop (#5123)

`loom:curating` gained the same TTL + standdown-marker + bounded-fallback
mechanism as `loom:reviewing`/`loom:treating` — `.claude/commands/loom/curator.md`'s
"Stale `loom:curating` Claim Check" — but **only** on the agent-side fast
path. Unlike the PR-side claims above, `reconcile_pr_claims` (and
`reconcile_workspace`) do **not** reconcile `loom:curating` at all: there is
no always-on daemon backstop for a dead Curator claim today. A stranded
`loom:curating` claim is only reclaimed when another Curator pass happens to
revisit the same issue (Priority 1/2 discovery, a re-curation pass, or an
explicit `/curator <number>` naming it) and runs the stale-claim check —
there is no periodic sweep that reclaims it in the background the way
`reconcile_pr_claims` does for `loom:reviewing`/`loom:treating` or
`reconcile_workspace` does for `loom:building`. `LOOM_STALE_CURATING_MINUTES`
(default 30, matching `LOOM_STALE_REVIEWING_MINUTES`) and
`LOOM_MAX_STANDDOWN_STREAK` are shared with the Judge/Doctor convention
above. Extending `claim_reconciliation` to cover `loom:curating` as a fourth
surface (mirroring `reconcile_pr_claims`'s `decide_pr`, keyed off the same
`labeled` timeline event) is a natural follow-up but was not required to
close #5123 — the agent-side check is a complete fix on its own, just with a
longer worst-case tail (bounded by how often issues in the Curator's queues
get revisited) than a PR-side claim has.

### Stale-verdict reconciliation (`loom:pr` / `loom:changes-requested`, #5686)

The passes above ask *"is the **claimant** still alive?"*. This one asks a
different question about a different label class: *"is the **tree** still the
one that was reviewed?"*

`loom:pr` and `loom:changes-requested` are **terminal verdicts**, and a verdict
is a statement about a specific tree. Before #5686 the label outlived the tree:
a rebase or force-push replaced every commit the verdict was written about and
the label sat there unchanged. Two failure modes:

- **A stall.** rjwalters/repo#192 (2026-08-08): Judge correctly requested
  changes at 02:22 for a genuinely-failing test; the branch was rebased and
  force-pushed at 02:55, turning CI green; the PR then sat carrying
  `loom:changes-requested` with nothing re-queueing it — the label said a
  verdict had already been rendered, so no Judge reclaimed it, and an operator
  had to clear it by hand.
- **A stale approval — the dangerous direction.** A `loom:pr` that survives a
  force-push lets Champion auto-merge a tree no Judge ever reviewed.

Judge stamps every verdict comment with

```
<!-- loom:verdict-sha sha=<head-sha> verdict=approved|changes-requested -->
```

(`.claude/commands/loom/judge.md` → "Verdict SHA Marker"), recording which tree
the verdict covers. `claim_reconciliation::forge::reconcile_pr_verdicts` runs on
the same periodic tick as the claim passes: for each open PR carrying a verdict
label it reads the newest marker **of that verdict's own kind** and compares its
SHA to the PR's current `headRefOid`. On a mismatch it posts an auditable
old→new-SHA comment, then swaps the verdict label (plus the per-tree companions
`loom:ci-failure` / `loom:merge-conflict`) for `loom:review-requested`.

| Property | Behavior |
|----------|----------|
| Kill switch | `LOOM_VERDICT_STALENESS_RECONCILE` (`0`/`false`/`no`/`off` disables). Defaults **ON** — it is corrective, not a feature gate — and is nested inside the master `LOOM_STALE_CLAIM_RECONCILE` switch. |
| No marker for the held verdict kind | `Keep(Unverifiable)` — **fail safe, never cleared**. Every verdict written before this shipped is in that state, so the pass is inert on rollout instead of force-clearing the queue. Since #6319 it is also **counted and anchored** rather than silently kept — see below. |
| Marker of a *different* verdict kind | Ignored. A PR rejected at SHA A and later approved at SHA B carries both; only the marker matching the currently-held label describes the current verdict. |
| Head SHA unreadable | `Keep(NoHeadSha)` — fail safe. |
| `loom:blocked` / `loom:operator` / `loom:operator-only` | `Keep(Held)` — still stale, but clearing would silently un-park a PR an operator (or Champion's capped-PR recovery pass) deliberately held. |
| Force-push vs. new commits | Not distinguished, deliberately. Any head move invalidates the verdict; an appended commit is as much "not the tree that was reviewed" as a rebase. |

#### Anchoring an unmarked verdict (#6319)

Failing safe on a missing marker is correct, but it is not a resting state: an
unmarked verdict is *permanently* unverifiable, so it keeps the full pre-#5686
hazard for the life of the label. And the marker is prose-compliance, not a
mechanism — judge.md *asks* the model to append it at ~19 separate verdict-write
sites, and production dropped it on roughly one verdict in four (same judge
identity, same 90-minute window). One observed unmarked approval was auto-merged
24 seconds later.

So `reconcile_pr_verdicts` now **remediates** `Keep(Unverifiable)` instead of
merely tolerating it: `decide_anchor` / `anchor_verdict` post a comment carrying
the missing marker, recording the head SHA as of that tick.

| Property | Behavior |
|----------|----------|
| Kill switch | `LOOM_VERDICT_ANCHOR` (`0`/`false`/`no`/`off` disables), nested inside `LOOM_VERDICT_STALENESS_RECONCILE`. Defaults **ON**. |
| Labels | **None are written.** Anchoring cannot approve, reject, or un-park anything — the verdict label stays exactly as it was; the only state that changes is that the verdict becomes invalidatable. |
| Already marked | Never touched (`Skip(AlreadyAnchored)`) — an already-marked verdict behaves byte-for-byte as it did before #6319. |
| Held PR | `Skip(Held)` — its comments are never fetched, and a PR a human parked should not collect automated comments either. |
| Comment fetch failed | `Skip(MarkerScanFailed)` — a failed fetch is indistinguishable from "no marker"; anchoring on it would post one duplicate comment per tick for the length of an API outage. |
| Idempotency | The marker posted is exactly what `extract_latest_verdict_sha` scans for, so the next tick reads `Fresh` and never anchors twice. |
| Counters | `VerdictReconcileStats { checked, invalidated, unverifiable, anchored }`. `unverifiable` counts only PRs whose comments were positively read, and the residual (`unverifiable - anchored`) is logged at `warn` — before #6319 this outcome had no counter anywhere in the daemon. |

**What anchoring does not do**: it cannot reconstruct which tree was actually
reviewed. A head move *before* the anchor is unrecoverable, and the verdict then
reads `Fresh` against a tree nobody read. It bounds future exposure from
"forever" to "one tick"; it is a backstop for judge.md's marker, never a
substitute for it.

**Agent-side fast paths** (same complementary relationship as the claim passes,
and they share the guard script `.loom/scripts/verdict-staleness-guard.sh`, which
takes `--clear` / `--anchor` and reports
`FRESH`/`UNVERIFIABLE`/`STALE`/`ANCHORED` via exit codes `0`/`11`/`12`/`13`):
judge.md's "Stale-Verdict Sweep" (step 0 of every pass),
doctor.md's "Stale-Verdict Check" (before claiming from either priority queue),
and champion-pr-merge.md's "Verdict-State Janitor → Part 2" (before the 6 safety
criteria — the gate that stops a stale approval from auto-merging).

Log line on action: `claim_reconciliation: cleared stale loom:pr from PR #N in
<root> (verdict recorded for <old>, head is now <new>) — re-queued as
loom:review-requested (#5686)`.

### Startup capacity seed: adopting live survivors (#6262)

The passes above answer "is this *dead* claim reclaimable?". The mirror-image
question — "is this *live* sweep still occupying a slot?" — is answered at
daemon startup by `startup_adoption::seed_capacity_from_journal`, which reads
the same journal.

A restart deliberately leaves in-flight sweeps running (they are detached
children) while rebuilding capacity accounting from scratch, so every survivor
the rebuild misses is a slot the work finder reads as free and refills. The
primary rebuild is `SweepRegistry::reconstruct()` from each root's
`.loom/locks/issue-<N>/owner.json`, and it stays primary — only it can recover
the sweep id, dispatch timestamp, token attribution, runtime, and pgid. Its
blind spot: a survivor whose lock did **not** survive. `reconstruct` deletes a
lock dir whose `owner.json` is missing or unparseable without asking whether a
process is still running, and a lock released while the child lived leaves no
lock at all. Nothing later re-adopts such a sweep — the reconciliation passes
above act on provable *death*, never on liveness.

The startup pass runs once, before the work finder / epic supervisor / role
runner / drain supervisor are spawned, and for every `effective_roots()`
workspace:

1. Provisions the root's `SweepRegistry`, so the lock-based `reconstruct()`
   runs at one deterministic point rather than as a side effect of whichever
   consumer touches that root first.
2. Unions in any journal record for that root whose pid is still alive and
   which the lock pass did not already recover, as a `Running` entry.

Union, never replacement: an issue the lock pass already tracks is skipped, so
occupancy is never double-counted. Adopted entries carry `pgid: None` (the
journal records no process group, so signalling degrades to single-pid — the
same conservative choice `reconstruct` makes for an unverifiable group) and
`token_name`/`runtime` of `unknown`. The ordinary reaper retires an adopted
entry when its pid exits.

**Observability.** `loom-daemon status` prints an `Adopted N surviving sweep(s)
from the machine journal at startup (#6262)` line, and `status --json` carries
`journal_adopted_at_startup`, **only when non-zero**. Zero is the healthy shape
and means either an idle host or — the common case — that every claim lock
survived and `reconstruct()` recovered all of them; a non-zero value is the
signal that the lock-based path came up short and the safety net carried the
difference.

## Stacked-PR dependency — #3729 (v1), #3747 (v2 item 1)

Stacked-PR mode pipelines a genuine dependency: when issue B consumes issue
A's output, B is built on `feature/issue-A` so B's Curator→Builder→Judge runs
concurrently with A's review instead of serializing behind A's merge. **The
dispatch surface is opt-in, daemon-`dispatch_sweep`-only, and
linear-chains-only.**

**Dispatch a chain** — N independent `dispatch_sweep` calls, each naming its
immediate predecessor via `depends_on` (there is no multi-node planner). Always
pass `workspace_root` explicitly — omitting it can silently target the wrong
managed workspace (#4503; see workspace_root note above):

```text
WORKSPACE_ROOT=$(git rev-parse --show-toplevel)
dispatch_sweep  kind={"Issue": A}  workspace_root=$WORKSPACE_ROOT                    # parent (independent)
dispatch_sweep  kind={"Issue": B}  depends_on=A  workspace_root=$WORKSPACE_ROOT      # child stacked on A
dispatch_sweep  kind={"Issue": C}  depends_on=B  workspace_root=$WORKSPACE_ROOT      # A→B→C linear chain
```

The daemon forwards `depends_on` to the child as `--depends-on <parent>`; the
child's Builder branches its worktree off `feature/issue-<parent>` (via
`worktree.sh --base`) and opens its PR with `--base feature/issue-<parent>`.
`depends_on` is `Option<u32>` — a **single** optional parent — so diamonds /
multi-parent stacks are structurally unrepresentable (no runtime rejection
needed). It is recorded on the `SweepInfo` entry for observability.

**Block-the-subtree on parent failure (reaper).** When a parent sweep reaches
a terminal state and its issue carries `loom:blocked`, the reaper emits
`sweep.issue.{child}.blocker` on the existing frozen topic (#3453 — no new
topic) for every live child whose `depends_on` names that parent, so the stuck
stack surfaces to the operator and the child does not auto-progress. This is
implemented via `SweepRegistry::children_of` + `block_children_of`. Auto-detach
(rebasing an orphaned child onto the default branch) is **out of scope for v1**.

**Reconciliation is triggered automatically on parent merge (v2 item 1,
#3747).** Because the repo squash-merges, after the parent squash-merges the
child branch still carries the parent's pre-squash commits. `merge-pr.sh` now
fires reconciliation automatically at its post-merge choke point (alongside the
partial-increment label reset, before branch deletion): it discovers open child
PRs via a **live forge query** (`gh pr list --base feature/issue-<parent>` — not
the daemon registry, whose terminal entries are GC'd ~1h after transition and
which only exists when `loom-daemon` is running), then per child splits
safe/unsafe on the child **issue's** `loom:building` label (fresh, uncached `gh
api` read):

- **Safe** (child issue not `loom:building`): invokes
  `./.loom/scripts/reconcile-stack.sh <child-pr> feature/issue-<parent>`
  (`git rebase --onto <default> <parent-branch> <child-branch>` +
  `--force-with-lease` + `gh pr edit --base <default>`).
- **Unsafe** (child issue still `loom:building`): a live Builder likely holds
  the child branch checked out, so the auto-rebase is **skipped** and a comment
  is posted on the child PR flagging deferred reconciliation. A later
  parent-merge-triggered pass (once the issue is no longer `loom:building`), or
  a manual run, picks it up.

The whole step is **best-effort** — a reconciliation failure (rebase conflict,
rejected force-with-lease, retarget failure) is logged as a warning and never
changes `merge-pr.sh`'s exit code (the parent merge already happened). It is
idempotent by construction: once a child's base is retargeted away from the
parent branch, the `--base` query returns zero rows on any re-run.

`reconcile-stack.sh` remains available for **manual** invocation — for the
unsafe/deferred case once the Builder finishes, or for an operator who wants to
reconcile ahead of a merge (`--dry-run` previews the git surgery).

A **pre-merge merge-ordering guard** shipped as v2 item 2 (#3747): because
`delete_branch_on_merge:true` deletes `feature/issue-<parent>` synchronously
during the merge API call — before the post-merge reconcile pass above can run —
`merge-pr.sh` now runs a guard *before* both merge paths that discovers open
child PRs (same `gh pr list --base feature/issue-<parent> --state open` query)
and by default **hard-blocks the merge** (`exit 1`, naming the child PR(s) + the
`reconcile-stack.sh` unblock command) rather than let the parent merge race the
branch deletion. It keys purely on "does an open child PR still target this
branch" (not the child's `loom:building` label). `--allow-stacked-children`
bypasses it; `--dry-run` reports the would-be block without exiting 1.

**Rebase-on-parent-amend** shipped as v2 item 3 (#3747): the standalone
`./.loom/scripts/rebase-stacked-children.sh feature/issue-<parent>` handles the
*pre-merge* case where Doctor amends a still-open stacked parent branch and a
child that branched off its pre-amend tip goes stale. It discovers open child
PRs with the same `gh pr list --base feature/issue-<parent> --state open` query,
detects staleness via `git merge-base --is-ancestor`, and rebases safe stale
children onto the parent's current tip (`git rebase` + `push --force-with-lease`,
base **not** retargeted — the child stays stacked), deferring children whose
issue is still `loom:building` with a comment. Doctor invokes it as a documented
best-effort step after pushing to a `feature/issue-<N>` branch. **Dependency
auto-detection**, **diamonds / multi-parent**, and **auto-detach** remain **out
of scope** (deferred items of the v2 epic #3747).

## Epic supervisor (#3842)

The **epic supervisor** (epic #3842) drives every open `loom:epic` issue
through a fork-join lifecycle autonomously. It runs as an opt-in loop on a
**dedicated OS thread** with its own current-thread Tokio runtime (`#3872`) —
never `tokio::spawn` on the shared daemon runtime — because each transition can
block on a minutes-long role process (`Command::status()`) while holding the
#3707 issue-creation mutex. Keeping that blocking call off the shared runtime
preserves the responsiveness of the event bus, reaper, sweep registry, and IPC
listener.

Enable it with `LOOM_EPIC_SUPERVISOR=1` (unset/false-y = OFF). Tunables:
`LOOM_EPIC_SUPERVISOR_INTERVAL_SECS` (default 300) and
`LOOM_EPIC_INFLIGHT_TTL_SECS` (default 900).

### Derived-state model

Rather than mint new GitHub labels per phase, all five supervisor states ride
the single `loom:epic` label and are **derived** — computed each tick from two
already-visible facts: the number of `### Phase` sections in the epic body, and
the open/closed status of the epic's `loom:epic-phase` children. The five states
(implemented as `EpicState` in
[`loom-daemon/src/epic_state.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/epic_state.rs)
(upstream Loom repo — not shipped to consumer installs)) are the derived
epic lane (#3841):

| Derived state | Condition | Enabled transition |
|---------------|-----------|--------------------|
| `epic:needs_decomp` | body has `< 2` `### Phase` sections | **decompose** — Architect enriches the body in place (no PR) |
| `epic:designed` | `≥ 2` phases, no `epic-phase` children yet | **expand** — Champion materializes phase-1 children (under the #3707 mutex) |
| `epic:active` | a current-phase child is open | per-child `/loom:sweep` dispatch (`BuildChildren`) |
| `epic:phase_join` | current phase's children all closed, more phases remain | **join** — Champion materializes phase N+1 children (mutex + barrier-gated) |
| `epic:done` | all phases' children closed, no phases remain | **close** — Champion closes the epic (terminal) |

### Transition table + phase-join barrier

The five intra-lane edges among the derived states — the "epic transition
table" — are declared explicitly in `epic_state::epic_transition_table()`:

```text
epic:needs_decomp → epic:designed    (Champion, creates_issues)   [decompose]
epic:designed     → epic:active      (Champion)                   [expand]
epic:active       → epic:phase_join  (Supervisor, barrier)        [fork-join]
epic:phase_join   → epic:active      (Supervisor, barrier)        [join/advance]
epic:phase_join   → epic:done        (Supervisor, barrier)        [close]
```

Every edge touching `epic:phase_join` is a **phase-boundary edge** and declares
a non-empty fork-join barrier
([`loom-daemon/src/phase_join.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/phase_join.rs)
(upstream Loom repo — not shipped to consumer installs)): the
barrier holds — degrading the plan to a no-op — until every child of the current
phase is closed, so phase N+1 (or epic close) never fires while a current-phase
child is still open.

The lane-*entry* edge `new → epic:needs_decomp` (an Architect filing a
`loom:epic` proposal) is **not** part of the supervisor's table — the supervisor
begins its lifecycle at `epic:needs_decomp`.

**Conformance.** `epic_transition_table()` is itself the authoritative model of
the epic lane (#4310) — there is no second, independently-maintained
implementation it is checked against. Its structural invariants (exactly five
states, `epic:done` the sole terminal state, exactly five intra-lane edges,
non-empty barriers on every `epic:phase_join` edge) are asserted directly by
[`loom-daemon/tests/epic_state_invariants.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/tests/epic_state_invariants.rs)
(upstream Loom repo — not shipped to consumer installs), which always runs (no
skip path).

### #3707 issue-creation mutex

The two issue-creating expand bursts (`decompose`'s downstream and both
`expand`/`join` Champion dispatches that run `gh issue create`) are serialized
through the global **#3707 issue-creation mutex**
([`loom-daemon/src/issue_creation_mutex.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/issue_creation_mutex.rs)
(upstream Loom repo — not shipped to consumer installs)).
The supervisor holds the async guard across the whole (spawn-and-wait) dispatch
so a burst never interleaves with any other issue-creating burst anywhere in the
daemon. All epic expands share the single `CHAMPION_EPIC_DECOMP` serialization
identity.

### Event topics

Each of the four singleton action-class transitions publishes an
`epic.issue.{N}.{action}` event on the shared event bus when it fires, so the
supervisor's decisions are tailable via `subscribe_to_events` /
`tail_event_bus`:

| Topic | Fires from | Payload |
|-------|-----------|---------|
| `epic.issue.{N}.decompose` | `epic:needs_decomp` | `{epic, action: "decompose", state: "epic:needs_decomp"}` |
| `epic.issue.{N}.expand`    | `epic:designed`     | `{epic, action: "expand", state: "epic:designed"}` |
| `epic.issue.{N}.join`      | `epic:phase_join`   | `{epic, action: "join", state: "epic:phase_join"}` |
| `epic.issue.{N}.close`     | `epic:done`         | `{epic, action: "close", state: "epic:done"}` |

The `BuildChildren` transition (per-child `/loom:sweep` dispatch) has **no**
epic-action topic — those dispatches already surface on the frozen
`sweep.global.dispatch` topic. Subscribe to `epic.issue` to receive every
epic-supervisor action across all epics, or `epic.issue.{N}` for one epic
(segment-aligned prefix match, same routing rule as the sweep topics).

### Cross-repo/cross-workspace complement: the Champion-side blocker check (#5211)

The supervisor above only reconciles epics **inside its own registered
workspace** — it can compute `epic:done` and auto-close an epic the moment its
last phase child closes, but only for an epic living in a repo where this
supervisor is itself enabled and running. It has no way to reach into a
*different* repo's epic before that repo's own dependent evaluates whether the
epic still blocks it — the exact cross-repo shape of the incident that
motivated #5211 (example-org/downstream-repo#101 blocked on example-org/tool-repo#202,
two different repos, neither running this supervisor).

`champion-common.md` → "Epic-Aware Blocker Check", wired into
`champion-issue-promo.md`'s Technical Feasibility criterion and
`champion-epic.md`'s phase-creation gate, closes that gap independently of
this supervisor: it asks the same "are this epic's phase children all closed"
question directly against the blocker's `loom:epic-phase` children **at
evaluation time, from the repo doing the evaluating** — no daemon
configuration required in the blocker's own repo, and it works whether or not
`LOOM_EPIC_SUPERVISOR` is set anywhere. The two are complementary rather than
substitutes: enabling this supervisor in an epic's own repo prevents the trap
at the source (a `Done` epic auto-closes before anything downstream can even
cite it as unpromoted); the Champion-side check is what actually resolves the
trap once it has already occurred, including across a repo boundary this
supervisor cannot cross.

## Autonomous work finder (#3810)

The **work finder** (Phase A of epic #3809,
[`loom-daemon/src/work_finder.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/work_finder.rs)
(upstream Loom repo — not shipped to consumer installs)) is the
daemon-native poller that turns a human-approved `loom:issue` into a dispatched
build **without an operator** — restoring the one capability the deleted v0.10.0
shepherd brain had that the daemon rebuild never replaced. It is **opt-in and
off by default**: unset `LOOM_WORK_FINDER` and the daemon's behavior is
byte-for-byte unchanged (the only sweep entry point remains the explicit
`DispatchSweep` IPC request).

Unlike the epic supervisor, the work finder runs as a plain `tokio::spawn`
interval task on the **shared daemon runtime** (the same footing as the reaper),
not a dedicated OS thread. Every call into `SweepRegistry::dispatch()` returns
promptly (fire-and-forget child spawn), so the loop never parks a runtime worker
in a long blocking call — the OS-thread machinery the epic supervisor needs for
its minutes-long spawn-and-wait role dispatches is unnecessary here.

Each tick:

1. Queries the forge for ready work — `gh issue list --label loom:issue --state
   open --limit 200 --json number,labels` (honoring `LOOM_REPO` for `--repo`).
2. Filters out issues that are **already in flight** (a live `Running` /
   `Pending` entry in the sweep registry — the authoritative dedup, robust to
   `loom:issue → loom:building` label-flip lag) or that defensively carry any
   skip label (`loom:building` / `loom:blocked` / `loom:operator-only`).
3. Dispatches the remainder through the existing `SweepRegistry::dispatch()`
   path — up to a **work-driven dynamic cap** (Phase B, #3811) recomputed every
   tick and counted against the current live sweep occupancy. `dispatch()`
   already flips `loom:issue → loom:building`, acquires the per-issue
   `mkdir`-atomic claim lock, and spawns the rotated-token child, so the finder
   reimplements none of the race guard. Each dispatch uses a
   `workfinder-<issue>` idempotency key, making a re-dispatch of an
   already-running issue a no-op.

### Dynamic concurrency scaling (Phase B, #3811; CPU/load term #3978; token axis removed / RAM added #5270)

The concurrency cap is **not** a fixed value resolved once at startup. Every
tick the finder recomputes

```
dynamic_cap = min(disk headroom, ram headroom, configured maxConcurrent)
```

from live inputs, so disk/RAM/backlog changes are honored without a daemon
restart. **`configured maxConcurrent` is the one term in that `min(...)` this
does *not* apply to (#6203):** `autonomous.workFinder.maxConcurrent` /
`LOOM_WORK_FINDER_MAX_CONCURRENT` is resolved once at daemon bring-up and
threaded into the loop as a frozen value — only the `disk headroom` / `ram
headroom` terms around it are re-read live each tick. Editing the config key
takes effect only after a daemon restart; see the `autonomous.workFinder.maxConcurrent`
row in the config reference table (below, under "Config surface") for the
full mechanism and the startup log line that names the resolved value and its
source (env / config / default).

> **This cap bounds SWEEP dispatch only (#6102).** Role-runner agents
> (Curator / Judge / Doctor / Champion / Guide / …) are spawned by the role
> runner's own interval and idle-edge loops and **never pass through this
> admission path**, so none of the three terms above — including
> `maxConcurrent` — has ever bounded them. Since #6102 they have their own
> ceiling, `autonomous.roleRunner.maxConcurrent` (default: the number of
> interval-cadence default roles); this host's worst-case agent count is
> `dynamic_cap + roleRunner.maxConcurrent`. See
> [The other half of the agent budget](#the-other-half-of-the-agent-budget-role-runner-agents-6102). **#5270 removed the token axis from this formula entirely**, on any
auth path (API key or subscription pool, overage-enabled or not) — operator
direction: "we should only ever limit parallelism based on the machine
disk/RAM/CPU." A metered `ANTHROPIC_API_KEY` has no 5h/7d subscription window,
and overage means a subscription pool no longer hard-stops at one either, so
counting *healthy accounts* was never a proxy for this host's actual capacity
to run more sweeps. `.ranking` health is **not** deleted — it still drives
spawn-time **selection** (`tokens_pool::select`: prefer fresher/healthier
accounts, skip revoked/blocked ones) and the account-health figures remain on
the status/calibrate surface as informational fields — it just no longer
gates *how many* sweeps may run concurrently. A fourth **cpu/load headroom**
term also sat in this `min(...)` from #3978 until **#4512 deleted it** — see
[Why there is no CPU term in admission](#why-there-is-no-cpu-term-in-admission-4512)
below. There is still no CPU *term* in the numeric cap; instead, since #4903
(and retuned by #5270 into the primary "dumb mode" CPU gate — default
threshold `0.95` load/core, down from the original `4.0` generous backstop)
the [saturation admission brake](#saturation-admission-brake-4903) *holds* new
admissions when the host is measurably saturated, without resizing the cap or
touching running work.

| Input | Source | Bound it enforces |
|-------|--------|-------------------|
| **disk headroom** | `floor(free_gb / LOOM_PER_WORKTREE_GB)` on the worktree-root volume (`disk_headroom::disk_headroom_limit`, a Rust port of `disk-headroom.sh` that shells to `df -Pk`) | never provision more worktrees than the scratch volume can hold |
| **ram headroom** (#5270) | `floor(available_gb / LOOM_PER_WORKTREE_RAM_GB)` on the host's currently-available memory (`ram_headroom::ram_headroom_limit`, modeled on `disk_headroom`'s shape: `/proc/meminfo`'s `MemAvailable` on Linux, `vm_stat` free+inactive pages × page size on macOS) | never provision more worktrees than available RAM can hold; the second "dumb mode" machine-headroom axis alongside disk |
| **configured maxConcurrent** | `LOOM_WORK_FINDER_MAX_CONCURRENT` / `autonomous.workFinder.maxConcurrent` (repurposed from Phase A's fixed target into an operator ceiling) | the per-machine **sweep-dispatch** admission knob (#4512) — tuned empirically by the operator, the only *policy* term in the `min(...)` (the other two meter exhaustible resources: bytes of disk and bytes of RAM). **Not the whole host's agent budget**: role-runner agents are admitted outside this formula entirely (#6102) |

Retired as cap inputs (informational-only now — see `capacity::token_axis_limit` / `tokens_pool::select`):

| Input | Source | What it still does |
|-------|--------|-------------------|
| **healthy-token count** (retired from the cap, #5270) | `available` accounts in `.ranking` in the pool directory `tokens_pool::paths::resolve_tokens_dir` resolves for the workspace — per-repo `{workspace}/.loom/tokens/` when it holds `*.token` files, else the shared machine-level pool (#3938) (`capacity::read_ranking` / `token_axis_limit`, unified with the writer in #4344) | drives spawn-time account **selection** (prefer fresher/healthier accounts, skip exhausted/blocked ones, #3902) and is reported on `status`/`calibrate` for observability — no longer bounds `dynamic_cap` |

**Per-token concurrency** (`LOOM_PER_TOKEN_CONCURRENCY` / `autonomous.perTokenConcurrency`, #3947) was retired from the cap by #5270 and then removed entirely by #5743 — it fed only a disclaimed `healthy × per-token` status/calibrate figure with no admission effect, which caused mis-diagnosis on the fleet more than once. The knob, its env var, and the status line are gone; a config file that still sets `autonomous.perTokenConcurrency` parses fine (unknown keys are ignored, not an error) but the key does nothing.

#### Why there is no CPU term in admission (#4512)

From #3978 until #4512 the `min(...)` carried a fourth term:

```
cpu_headroom = max(1, floor((logical_cpus × cpuUtilizationTarget − consumed_cores) / estCoresPerSweep))
```

It was built for a real incident — 2–3 concurrent Rust builds in sweep
worktrees starved `build-gate.sh` of CPU badly enough that it hit its own 600s
timeout, which the (separately-fixed, #3974) gate misreported as a verified-red
`main`, halting all dispatch — and hardened over #4031/#4234. **#4512 deleted
it anyway**, because it had become the binding constraint on fleet throughput:

- It priced **every** sweep as a build (`estCoresPerSweep`). Sweep wall-clock is
  dominated by **API wait** (curator / builder / judge conversations); the
  sustained-heavy-CPU phases (release builds, full test suites, the build gate)
  are a small fraction of it. So the term throttled the ~90% low-CPU majority to
  defend against the minority case.
- Measured evidence (8-core EC2 worker, 2026-07-29): the host sat **95% idle**
  while the formula capped concurrency at **2**, and `loom-daemon calibrate`
  recommended "no change" — the formula could not see past its own estimate.

The replacement is two-part, and the protection moved to **where the load
actually is**:

1. **Sweep admission is one per-machine knob.** `autonomous.workFinder.maxConcurrent`
   is the whole *sweep* policy, tuned empirically by the operator (expect 10+ on an
   8-core API-bound worker). The two remaining terms — disk headroom and (since
   #5270) RAM headroom — stay because they meter genuinely *exhaustible*
   resources (bytes), not an estimate. (The token axis sat here too until
   #5270 removed it — see the section above.) **It is not the whole *host*
   policy**: role-runner agents are admitted outside this formula entirely and
   carry their own ceiling since #6102 — see
   [The other half of the agent budget](#the-other-half-of-the-agent-budget-role-runner-agents-6102).
2. **The genuinely heavy stages serialize where they occur**, via the
   [machine-wide build slot](#machine-wide-build-slot-4512). N sweeps run
   concurrently; at most `LOOM_BUILD_SLOTS` of them hold the slot while running
   `cargo build`/`test` or the build gate; the rest of a sweep's lifecycle never
   queues.

The **host-distress circuit breaker** (#4235) is unchanged and is what makes a
hand-tuned knob safe to raise: a mis-set `maxConcurrent` trips a **measured**
breaker (load-per-core), instead of melting the host on the strength of a
mis-estimated constant. #4903 added a third guard on the same measured signal —
the point-in-time [saturation admission brake](#saturation-admission-brake-4903)
— after a CPU-heavy (analog-simulation) workload reached 12× overcommit on a
host whose `maxConcurrent` was never set.

`cpu_headroom.rs` survives as a pure **measurement** module — logical core
count, load average, measured idle fraction, and the `is_host_saturated`
predicate — consumed by the host breaker (#4235), the build gate's load-aware
deferral (#4259), and the `status` / `calibrate` reports. Nothing it produces
feeds the cap.

**Retired knobs are accepted-but-ignored, never a config error.**
`autonomous.cpuUtilizationTarget` / `autonomous.estCoresPerSweep` and their env
twins `LOOM_CPU_UTILIZATION_TARGET` / `LOOM_EST_CORES_PER_SWEEP`
(`work_finder::DEPRECATED_CPU_CONFIG_KEYS` / `DEPRECATED_CPU_ENV_VARS`) still
parse — a fleet upgrades the daemon binary before it edits every repo's
committed `.loom/config.json`, so a stale key must be a *visible no-op*, not a
parse failure. One message
(`work_finder::deprecated_cpu_knob_notice`) names exactly which config keys and
env vars to delete, delivered over the channel the reader is actually watching:

- **daemon** — `work_finder::warn_deprecated_cpu_knobs` logs it **once per
  process** at startup, into `~/.loom/daemon.log`.
- **CLI** — `loom-daemon calibrate` prints it to **stderr**. A CLI subcommand
  returns from `main` *before* `setup_logging()` runs, so a `log::warn!` there
  would go nowhere; stderr also keeps `--json`'s stdout payload clean.

Deleting the settings silences it; leaving them changes nothing.

*Consumed cores come from a measured idle fraction, not the load average
(#4031).* This detail still governs what `status`/`calibrate` **report** (and
fed the deleted term while it existed). The original #3978 term used the
1-minute load average as a stand-in for consumed cores. On macOS that overstated
consumption by ~1.5× — an observed idle-but-loaded host read `load1m ≈ 6–7`
alongside 76–86% CPU idle on 28 cores (only ~4–7 cores actually consumed). Load
average counts threads in the runnable **and** uninterruptible-sleep states, and
this daemon's workload is dominated by `claude` sessions **blocked on network
I/O**, which inflate load without consuming a core — pointing the (then)
feedback loop the wrong way (more concurrent sweeps → more blocked `claude` →
higher load → *lower* cap, while CPU sat idle). The reported CPU consumption is
therefore `logical_cpus × (1 − idle_fraction)` from a measured idle fraction,
**per-platform**:

- **Linux** deltas two reads of the aggregate `cpu` line in `/proc/stat`
  (`idle + iowait` vs total), memoizing the previous cumulative sample and
  deltaing **across ticks** — nothing sleeps.
- **macOS** shells to `iostat -c 2 -w 1 -n 0` and reads the **second** data
  line (a genuine 1-second delta; the first is cumulative since boot). That
  1-second wait is moved to `spawn_blocking` at the async call sites and
  **memoized** behind a ~10s TTL (`CPU_UTIL_MEMO_TTL`) shared with the
  synchronous `ipc.rs` status path, so a runtime worker is never blocked and a
  status request + a work-finder tick never each pay the full second.

Every read here is **fail-open**: an unreadable idle signal (missing
`/proc/stat` or `iostat`, unsupported platform, or no delta sampled yet) is
`None`, and every consumer must fail *safe* on `None` rather than assume "host
fully loaded" — `is_host_saturated` returns `None` so the build gate runs
instead of deferring, and the host breaker treats a missing sample as a
non-observation. This mirrors `capacity::token_axis_limit`'s policy of falling
back to the optimistic basis when no ranking data exists: the absence of a
signal is not evidence of unhealthiness.

**`LOOM_PER_WORKTREE_GB` deliberately stays env-only (#4032 decision).**
`disk_headroom`'s `LOOM_PER_WORKTREE_GB` was *not* migrated to the
`autonomous.*` config pattern. It has a second, independent
Bash-side reader (`defaults/scripts/lib/disk-headroom.sh`, consumed by
`/loom:sweep` Stage -1 wave sizing) alongside the Rust
`disk_headroom::per_worktree_gb`. Migrating only the Rust side would create a
live divergence where the same env var honors `.loom/config.json` on the
daemon path while silently ignoring it on the sweep path — worse than today's
consistent env-only behavior. Wiring the Bash `config-resolver.sh` into
`disk-headroom.sh` too is a separate, larger change; file a follow-up if that
cost is judged worth paying.

**RAM headroom mirrors this env-only shape (#5270).** `ram_headroom::per_worktree_ram_gb`
resolves `LOOM_PER_WORKTREE_RAM_GB` (default **2** GB) the same way
`disk_headroom::per_worktree_gb` resolves its own env var — no config key,
env-only, floored to a minimum of 1. There is no Bash-side RAM counterpart to
`disk-headroom.sh` (RAM headroom is Rust-only, consumed solely by the daemon's
own `resolve_dynamic_max_concurrent`), so the #4032 divergence concern above
does not apply here — env-only was simply the cheapest consistent choice, not
a workaround for a second reader.

#### Saturation admission brake (#4903)

Deleting the CPU term left admission with **no term that reads the host at
all**. That is correct for the workload #4512 measured, and wrong for a workload
it did not anticipate. `loom-worker-1` (8 vCPU) was observed at **load average
95** — `11.9` load/core, `0.07%` idle — from only **three** in-flight sweeps,
because all three were analog-simulation repos (`analog-*`) that had spawned 16
`ngspice` processes between them. SPICE simulation is sustained CPU for tens of
minutes, not API-wait, so for those repos a sweep is closer to a build than to a
conversation. The daemon *measured* the saturation (`loadavg_1m` and
`cpu_idle_fraction` are both in the dispatch headroom report) and did not act on
it: `capacity_bound: false`, cap `12`, nine more slots nominally free.

The fix is a **brake on admission, not a return to CPU-based sizing**. Once per
work-finder tick the daemon asks `cpu_headroom::is_host_saturated` — the same
predicate the build gate's load-aware deferral uses — whether load-per-core is
at/over a hold threshold, and if so it **holds new admissions** for that tick
and re-checks on the next one. It does not resize the cap, does not estimate
per-sweep cores, and **never** touches work that is already running.

| | Value |
|---|---|
| Config | `autonomous.workFinder.saturationBrake.enabled` / `.loadPerCoreHold` |
| Env | `LOOM_ADMISSION_BRAKE` / `LOOM_ADMISSION_BRAKE_LOAD_PER_CORE` |
| Default | **on**, hold at **0.95** load/core (retuned from `4.0` by #5270) |
| Precedence | env > config > default, resolved once at daemon startup |

**Retuned by #5270 from a generous backstop to the primary CPU admission
gate.** Until #5270 the token axis and disk headroom were the two hard
admission floors, with this brake acting only as a rarely-tripped safety net
well above them (`4.0` load/core — four runnable threads per logical core).
#5270 removed the token axis from admission entirely, which promotes this
brake to **the** CPU term in "dumb mode": its default now matches the
operator's literal ask — hold new admissions once the host's few-minute load
average reaches ~95% of its logical core count (`0.95` load/core), resume
below it.

Properties that are load-bearing (and are pinned by tests):

- **In-flight sweeps are never killed or preempted.** The brake is applied only
  where *new* candidates are admitted, immediately before the concurrency-cap
  check; it has no path to the reaper or to any running child. A held tick
  dispatches nothing and returns, and the running sweeps drain normally — which
  is precisely how the host recovers.
- **A healthy host is unchanged.** Below the threshold the admission path is
  byte-for-byte the pre-#4903 one, so an idle 8-core host still reaches its
  configured cap. `0.95` holds a notch below full saturation (`1.0` = as many
  runnable/uninterruptible threads as logical cores), mirroring the build
  gate's own `0.9` deferral point rather than the pre-#5270 `4.0`, which would
  have let a host run at 4× overcommit before this brake ever engaged — the
  incident that originally motivated the brake was at `11.9`.
- **Fail-safe on missing data.** No load-average source (or a zero CPU count)
  ⇒ no hold. Absent evidence is never treated as load.
- **Nothing latches.** The hold is re-evaluated every tick and releases the
  moment load drops back under the threshold.

**How this differs from the host-distress circuit breaker (#4235)** — the two
read the same normalized ratio and are complements, not duplicates:

| | Admission brake (#4903) | Host breaker (#4235) |
|---|---|---|
| Nature | point-in-time: one reading, one decision | stateful: trips only after N consecutive over-threshold ticks |
| Default threshold | `0.95` load/core (`4.0` before #5270) | `2.5` load/core |
| Release | immediate, first tick under the line | after a 5-minute cool-down |
| Explicit `dispatch_sweep` | never blocks it | hard-blocks unless `force` |
| Purpose | stop *adding* to a full host | remember a *meltdown* so a load dip cannot re-admit a whole wave |

**Since #5270 the brake's default threshold sits *below* the breaker's** — the
reverse of the pre-#5270 ordering. Pre-#5270 the brake decided from a single
reading at a generous `4.0` and had to "be sure" before firing; post-#5270 it
is the fast, cheap, per-tick hold that engages first (a single over-threshold
reading, `0.95`), with the breaker remaining the slower, stickier trip for
genuine sustained distress (several consecutive over-threshold ticks, `2.5`)
well past the point the brake has already held new admissions.

**Visibility.** `loom-daemon status` prints an `Admission brake:` line
(`HOLDING …` / `OK …` / `disabled`), `--json` carries an `admission_brake`
object, and while the brake is holding the capacity block's
"not capacity-bound … the limiter is work availability" line is **replaced** by
an `⚠ ADMISSION BRAKE HOLDING` line naming the host as the limiter. Per-tick
deferrals are counted as `deferred (host saturated)` in the work-finder tick log
and as `deferred-saturation` in `loom-daemon health`. A host holding back says
so; it no longer reads as idle.

##### Starvation escape hatch (#5715)

The brake's release condition — "the very next reading drops back under
threshold" — assumes something *the brake itself is blocking* is what is
holding the load up, so simply waiting eventually relieves it. That
assumption breaks when the load is generated entirely by work the brake has
**no authority over** — most notably the role runner's own
champion/curator/judge/doctor/guide ticks (which are *not* gated by this
brake). On `studio-host` that livelocked sweep admission for **33 hours**:
held forever, load never dropped (because it was all role-runner load), zero
sweeps in flight the entire time, and the per-tick `deferred (host
saturated)` INFO counter looked identical to one healthy backpressure tick.

The daemon now folds the current in-flight sweep count into every
observation (`crate::ipc::count_in_flight_sweeps` in the production
multi-workspace loop) so it can tell "held, sweeps genuinely draining"
(healthy backpressure) apart from "held, **zero** sweeps in flight,
continuously" (starvation — nothing running can ever relieve the load). Two
bounded reactions follow, purely from that one signal:

| | Config | Env | Default | Effect |
|---|---|---|---|---|
| Escalating log | `autonomous.workFinder.saturationBrake.starvationWarnSecs` | `LOOM_ADMISSION_BRAKE_STARVATION_WARN_SECS` | `300` (5m) | After this many seconds of continuous held+0-in-flight, a `WARN`-level `admission_brake: STARVING …` line fires once per streak, naming the elapsed duration |
| Escape hatch | `autonomous.workFinder.saturationBrake.starvationEscapeSecs` | `LOOM_ADMISSION_BRAKE_STARVATION_ESCAPE_SECS` | `900` (15m) | After this many seconds, the brake yields for **exactly one tick** — held reports `false` even though the raw load reading is still over threshold — logged at `ERROR` as `admission_brake: STARVATION ESCAPE HATCH …` |

The escape hatch does not disable the brake or bypass #5270's "dumb mode"
gate: it is a periodic, bounded safety valve. The starvation streak resets
immediately after an escape (a fresh full window is required before the next
one), and any tick with even one sweep in flight resets it too — so genuine
backpressure, however long it holds, never trips either reaction. Both
counters (`starving_ticks`, `escape_hatch_grants`) and `starving_since` are on
the `admission_brake` status/JSON block, and `loom-daemon status`'s
`Admission brake: HOLDING …` line appends a `⚠ STARVING` clause once the
current hold has zero sweeps in flight.

**#5715 is about starvation, not about bounding role-runner load.** The escape
hatch stops role-driven load from *permanently blocking sweep admission*; it
does nothing to bound or surface role-agent concurrency itself. That gap is
closed separately, by the ceiling described next (#6102), and the two
compose: the ceiling caps how much role-driven load can accumulate, the escape
hatch guarantees that whatever load does accumulate cannot livelock sweeps.

##### What the brake can and cannot hold back (#6102)

The brake's guarantee is narrower than "it protects the host from overload",
and stating it precisely matters because an operator reads this line when
deciding what to tune:

| | |
|---|---|
| **It samples** | host-wide load-per-core. A role agent's CPU absolutely counts toward the hold threshold |
| **It can hold back** | a **new sweep admission**, and nothing else |
| **It cannot hold back** | a role-runner agent — those are admitted by the role runner's own loops, bounded only by `autonomous.roleRunner.maxConcurrent` (#6102) |
| **It cannot shed** | anything already running. In-flight sweeps and in-flight role agents both run to completion; the brake is an *admission* gate, never a preemption mechanism |

Two practical consequences:

- **"Held with 0 sweeps in flight" does not mean an idle host.** The starvation
  log lines now say so explicitly, naming the live role-agent count when there
  is one (`… — NOTE: 3 role-runner agent(s) ARE running; the brake cannot hold
  those back …`). Before #6102 they asserted "nothing is running to relieve
  it", which was false in exactly the case that matters.
- **If load is high while few sweeps are in flight, lowering `maxConcurrent`
  will not reach that load.** Lower the role-agent ceiling instead.

#### The other half of the agent budget: role-runner agents (#6102)

`autonomous.workFinder.maxConcurrent` is the knob an operator reaches for after
a load-induced crash — and until #6102 it delivered materially less protection
than its own documentation implied, because **it bounds sweep dispatch only**.

On `studio-host` (Mac Studio M3 Ultra, 28 logical cores) an overnight hard halt
under 1m load averages of **126–136** was remediated by lowering
`maxConcurrent` from 16 to 8. Afterwards the host still measured:

```
In-flight sweeps: 1
Dynamic concurrency cap: 8
host cpu: 28 logical cores, 8% idle (~25.8 cores consumed), 1m loadavg 32.41
$ pgrep -f "claude-wrapper.sh" | wc -l
11
```

1.15 load-per-core — above the `0.95` brake threshold — with a *single* sweep
in flight against a cap of 8. The other ten agents were role-runner ticks
(25 registered workspaces × 7 interval roles = up to 175 potentially
concurrent), admitted entirely outside `min(disk, ram, maxConcurrent)`.

**The fix is a distinct ceiling for the distinct population.**

| | Value |
|---|---|
| Config | `autonomous.roleRunner.maxConcurrent` |
| Env | `LOOM_ROLE_RUNNER_MAX_CONCURRENT` |
| Default | the count of interval-cadence default roles (`role_runner::default_max_concurrent`) — **7** today |
| Precedence | env > config > default, re-read every tick (a config edit hot-applies, unlike `maxConcurrent`) |
| Scope | **process-wide across every managed workspace**, because the resource it protects (host CPU/RAM) is shared by all of them |

Design notes:

- **Counted across roots, resolved per root.** The count compared against the
  ceiling spans every managed workspace — a per-root ceiling would have bounded
  nothing on a 25-workspace host. The *value* comes from whichever root's tick
  is asking, exactly like `architectMaxProposals`; where roots disagree, the
  tighter root simply refuses sooner.
- **A derived default, not a magic number.** Defaulting to the number of
  interval-cadence default roles bounds role-agent load at roughly *one wave of
  distinct roles* instead of letting it scale with workspace count, and adding a
  role raises the ceiling by exactly one rather than silently squeezing the
  others.
- **Refusals are `WARN`, and distinct from cadence overlap.** A ceiling refusal
  logs `role_runner: <role> tick for <root> not admitted — N role agent(s)
  already in flight at the ceiling of M …` and retries next tick. The pre-existing
  per-`(root, role)` overlap skip (#4364) stays at `debug!` — it is routine
  cadence overlap, not a resource limit, and conflating the two is what made
  role-agent load invisible.
- **Not folded into `dynamic_cap`.** Sweeps and role agents are admitted by
  different subsystems against different queues; one `min(...)` over both would
  misreport which is actually binding. They are reported as two ceilings whose
  sum is the host's worst-case agent count.

**Observability.** `loom-daemon status` prints the live count and its ceiling
immediately under the in-flight sweep table, plus the total:

```
Role-runner agents in flight: 3 (ceiling 7)
  (a SEPARATE ceiling from the sweep cap below — autonomous.workFinder.maxConcurrent
   bounds sweep dispatch only; tune these agents with
   autonomous.roleRunner.maxConcurrent / LOOM_ROLE_RUNNER_MAX_CONCURRENT, #6102)
  total agents on this host = 1 sweep(s) + 3 role agent(s) = 4
```

`--json` carries the same under a `role_agents` object (`active`,
`max_concurrent`, `total_with_sweeps`,
`bounded_by_work_finder_max_concurrent: false`); a pre-#6102 daemon sends
neither field, which parses as `0` active and a `null` ceiling — read `null` as
**unknown**, not as "unbounded". `loom-daemon calibrate` reports the ceiling
next to `maxConcurrent` in its "Currently configured" block, and its one-line
reading appends the worst-case agent sum whenever the role runner is enabled.

#### Sizing `maxConcurrent`: per-machine **and** per-workload (#4512, #4903)

`autonomous.workFinder.maxConcurrent` is the only *policy* term in the cap, and
it is **not** a fleet-wide constant. It is a property of one machine **running
one kind of work**. Two hosts with identical core counts want very different
values, and the same host wants a different value if you move a different repo
onto it:

| Workload | What a sweep actually does | Reasonable `maxConcurrent` on 8 cores |
|---|---|---|
| **Software repos** (Loom itself, most product repos) | Dominated by API-wait — curator/builder/judge conversations. The heavy phases (release builds, full test suites, the build gate) are a small fraction of wall-clock, and they already serialize on the [machine-wide build slot](#machine-wide-build-slot-4512). | **10+** — the host sits mostly idle at lower values (#4512 measured 95% idle at a cap of 2) |
| **Analog / simulation repos** (`analog-*` running `ngspice`) | Dominated by sustained CPU — one sweep spawns ~5 simulator processes, each near 50% of a core, for tens of minutes. A sweep here is closer to a build than to a conversation. | **2–3** — at 12 the host reaches 12× overcommit and every sweep's simulations contend, so wall-clock per sweep grows faster than concurrency adds |

Consequences worth internalizing:

- **Unset is not "safe".** An unset `maxConcurrent` contributes a *default*, not
  an operator's judgement — which is exactly the input #4512's design depends on.
  The `loom-worker-1` incident happened on a host where it was never set.
- **Set it per machine, on the machine.** A repo's committed `.loom/config.json`
  is shared by every host that clones it; the per-machine value belongs in the
  machine-level tier (`~/.local/share/loom/config/defaults.json`) or in
  `.loom/config.local.json`, which is why the resolver merges four tiers.
- **Mixed hosts size to the heaviest tenant.** If analog and software repos share
  a host, one sweep is not one unit of load — tune down to what the heavy repo
  needs (per-repo weighting is deliberately *not* implemented; see #4903).
- **Throughput inverts past the knee.** Beyond the point where sweeps contend,
  adding concurrency makes everything slower. Raise the knob only while
  `loom-daemon status` shows the ceiling binding *and* the host measurably idle
  — that is exactly the pair
  [`loom-daemon calibrate`](#sizing-a-host-loom-daemon-calibrate-4390-measurement-only-since-4512)
  reports.
- **The brake is a backstop, not a substitute.** A correctly-tuned
  `maxConcurrent` should mean the saturation brake never engages. If it *is*
  engaging, that is the signal to lower the knob for this machine's workload —
  **unless few sweeps are in flight**, in which case the load is not coming
  from the population this knob bounds. Check `loom-daemon status`'s
  role-agent line before touching `maxConcurrent`: lowering it cannot reach
  role-driven load (#6102).

#### Machine-wide build slot (#4512)

Deleting the admission-time CPU term would leave nothing between N concurrent
`cargo test --workspace` runs and the host, so #4512 pairs it with a
**machine-wide build slot** — the bounded revival of #4003's "agents check out a
slot only for bound work". N sweeps run concurrently, but at most
`LOOM_BUILD_SLOTS` of them may be inside a designated high-CPU stage at once.

**Shape.** A slot is a lock **directory** created with `mkdir` (POSIX-atomic,
works across processes *and* across languages; `flock` is deliberately avoided —
unavailable on stock macOS). The primitive is `MkdirLock`, the same one the
token-pool state files and the per-issue claim lock (`.loom/locks/issue-<N>`)
use. Two implementations share one protocol so they serialize against each
other:

| Half | File | Used by |
|------|------|---------|
| Rust | `loom-daemon/src/build_slot.rs` (`acquire` / `BuildSlotLease`) | the daemon's main-health gate, around its gate command |
| Bash | `defaults/scripts/lib/build-slot.sh` (`loom_build_slot_acquire` / `_release`) | `build-gate.sh`, i.e. every sweep's post-Builder quality gate in its own worktree |

Slots live **machine-wide** at `~/.loom/locks/build-slot/slot-<i>` —
deliberately *not* under a repo's `.loom/locks/`. The host's cores are one
machine-level resource shared by every workspace the daemon manages, and #4512's
operator note asked explicitly for **one unambiguous home** for machine-tier
concurrency state rather than the ambiguous per-workspace tier (on a
multi-workspace host, "which `.loom/config.json` supplied the number" is a real
question).

**Three hard safety properties**, all load-bearing:

1. **Never deadlocks.** `acquire` waits at most `LOOM_BUILD_SLOT_WAIT_SECS`
   (default 300s) and then **degrades open** — it returns a lease that holds no
   slot and lets the caller run unserialized. A slow or crashed holder therefore
   costs throughput, never liveness.
2. **Degrades open on any lock-store failure.** An unwritable / missing /
   permission-denied lock directory yields a degraded-open lease *immediately* —
   no spinning, no error propagated to the caller. Refusing to build because the
   lock store is broken would be strictly worse than building unserialized.
3. **Re-entrant.** A holder exports `LOOM_BUILD_SLOT_HELD=1` into its child
   environment, so a nested acquire (the daemon's main-health gate holding the
   slot around `build-gate.sh`, which then tries to take it too) is a logged
   no-op instead of a self-inflicted wait on its own parent's slot.

Because a slot is held for the *duration of a build* (minutes), the stale-reap
threshold is deliberately long (`LOOM_BUILD_SLOT_STALE_SECS`, default 1h) —
`mkdir` locks carry no heartbeat, and a 30s threshold (the token-pool default)
would let a peer reap a perfectly healthy in-progress build. A crashed holder is
covered by property 1, not by aggressive reaping.

**Telemetry.** Every state transition is logged once: on acquire (naming the
slot and the wait), on the first wait of a round (so a queue is visible), on
release (with the hold duration), and on degrade-open (naming *why*).
`loom-daemon calibrate` reports the resolved slot count.

**Knobs are env-only**, following the `LOOM_PER_WORKTREE_GB` precedent above for
exactly the same reason: an independent Bash-side reader must resolve identical
values, and honoring `.loom/config.json` on one path while silently ignoring it
on the other is worse than consistent env-only behavior. Export them from the
daemon's start environment.

| Env var | Default | Meaning |
|---------|---------|---------|
| `LOOM_BUILD_SLOTS` | `1` | Concurrent build slots on this machine. `0` **disables** serialization entirely (every acquire degrades open) — exactly the pre-#4512 behavior for an operator who wants it back |
| `LOOM_BUILD_SLOT_WAIT_SECS` | `300` | Bounded wait before degrading open |
| `LOOM_BUILD_SLOT_STALE_SECS` | `3600` | Age at which an abandoned slot dir is reaped |
| `LOOM_BUILD_SLOT_DIR` | `~/.loom/locks/build-slot` | Slot directory override (test seam; also relocates slots onto a chosen filesystem) |
| `LOOM_BUILD_SLOT_HELD` | *(unset)* | Re-entrancy sentinel, set by a holder for its children — not an operator knob |

Default `1` because the heavy stages (`cargo build`/`test`, the build gate)
already parallelize across essentially every core on their own; one at a time is
the point. #4512 allows "at most 1–2".

**The #4231 release-build fan-out under this model.** #4231 was a 6-way sweep
fan-out that drove load to 118 on a 28-core host, because six concurrent
release-building sweeps looked like `6 × estCoresPerSweep(2.0) = 12` cores of
demand to a term whose real demand was closer to `6 × 28`. Under #4512 the same
fan-out admits 6 sweeps (the knob, not an estimate, decides that) but only **one
of them compiles at a time** — the other five block in `build-gate.sh`'s slot
acquire, so peak demand is one build's worth of parallel `cargo`, not six. The
formula that mis-estimated the fan-out is gone; the thing it was estimating is
now directly bounded. If the knob is nonetheless set too high for the machine,
the #4235 host breaker trips on **measured** load-per-core and suppresses
dispatch — a measurement, not a constant, is the last line of defence.

#### Sizing a host (`loom-daemon calibrate`, #4390; measurement-only since #4512)

Originally (#4390) this subcommand *recommended* — and with `--write` applied —
new knob values, deriving a `maxConcurrent` ceiling from the CPU headroom term
(`cpu_headroom + 25% slack`). #4512 deleted that term, so there is nothing left
to derive a recommendation *from*: `autonomous.workFinder.maxConcurrent` is now
the one per-machine knob, **tuned empirically by the operator**.

Calibrate is therefore strictly a **measurement report**. It measures the host
with the **same** primitives dispatch uses (`cpu_headroom::logical_cpu_count` /
`cached_cpu_idle_fraction` / `read_loadavg_1m`,
`disk_headroom::disk_headroom_limit`, `capacity::read_ranking` falling back to
`tokens::token_pool_size`, `build_slot::resolve_slots`), so it can never
disagree with what dispatch actually does:

```
loom-daemon calibrate                 # human-readable measurement report
loom-daemon calibrate --json          # machine-readable
loom-daemon calibrate --write         # DEPRECATED, ignored (prints a notice)
```

- **It never writes config.** `--write` is accepted-but-ignored with a
  deprecation notice so an existing scripted `calibrate --write` does not start
  failing. The JSON payload keeps a `"write": {"applied": false, "config_path":
  null}` object for machine consumers that keyed off it.
- **What it prints**: host measurements (logical cpus, observed idle fraction +
  1m loadavg, disk headroom, ram headroom, healthy/total accounts — informational
  only, build slots), the currently resolved knobs, the `min(disk, ram,
  maxConcurrent)` breakdown (#5270 — no token term), which term binds (`disk` /
  `ram` / `ceiling`), the **role-agent ceiling** and this host's worst-case
  agent count (#6102), and one line of advice.
- **How to read it** (this is the tuning loop that replaces the old
  recommendation):
  - binds on `ceiling` **while the host sits idle** ⇒ raise `maxConcurrent`;
  - binds on `ceiling` **while the host is saturated** (low idle fraction, or
    the host breaker tripping) ⇒ the knob is already at/above what this machine
    sustains — leave it or lower it;
  - binds on `disk` / `ram` ⇒ raising `maxConcurrent` changes nothing; free
    scratch space or memory.
- **`maxConcurrent` is only half the budget (#6102).** Every reading above
  concerns **sweep** dispatch. When the role runner is enabled, the advice line
  appends the role-agent ceiling and the worst-case sum
  (`maxConcurrent + roleRunner.maxConcurrent`) — because a host that is
  saturated with *few sweeps in flight* is not telling you to lower
  `maxConcurrent`; lowering it cannot reach role-driven load. Use
  `loom-daemon status`'s live role-agent count to tell the two apart.
- **Deprecation surface**: `calibrate` also prints the retired-knob notice to
  **stderr** — an operator running it to size a host is exactly who needs to hear
  that a stale `estCoresPerSweep` is doing nothing (see the previous
  subsection for why stderr, not the log, on this path).
- **Disk has no config key** — `LOOM_PER_WORKTREE_GB` is deliberately env-only
  (previous subsection) — so calibrate names the env-var alternative instead.
- **Env overrides win**: when `LOOM_WORK_FINDER_MAX_CONCURRENT` is set, the
  report says so explicitly — editing `.loom/config.json` has no effect until
  the env var is unset or updated (env > config > default).
- **It names *which file* set the knob** (`set by: …`, JSON
  `measurements.max_concurrent_source`). Now that one number carries the whole
  admission policy, "edit the config" has to be unambiguous: the effective config
  is a deep merge of up to four tiers
  (`config_resolver::tier_paths_by_precedence`) and the daemon manages several
  workspaces, so `maxConcurrent = 3` might be a repo's committed value, a
  host-local override, or the built-in default. `config_resolver::source_of`
  reports the **highest-precedence tier that actually sets the key**; `null` /
  `"built-in default"` means no tier does. When an env var shadows a tier the
  report says that too, so nobody edits a file that cannot take effect. For a
  genuinely machine-wide setting (one value for every workspace on the host),
  put it in the machine tier — `~/.local/share/loom/config/defaults.json`, or
  `LOOM_WORK_FINDER_MAX_CONCURRENT` in the daemon's start environment — rather
  than in each repo's `.loom/config.json`.
- **Fleet-safety**: a generous committed `maxConcurrent` is still safe fleet-wide
  because the two exhaustible-resource terms remain in the `min(...)` (each host
  binds on its **own** disk/RAM), the build slot bounds concurrent heavy
  builds per machine, and the host breaker is the measured backstop.
- **Startup hint**: `loom-daemon-start.sh` prints one advisory line at start
  time — `"maxConcurrent N binds while the host is M% idle — consider raising
  autonomous.workFinder.maxConcurrent"` — only when the ceiling is *currently*
  the binding term **and** the measured idle fraction is at or above 50%
  (`calibrate::IDLE_HEADROOM_FRACTION`). Never fatal: a missing `jq`, a
  calibrate error, or an unparseable payload all fall through silently,
  mirroring the advisory-only safehouse status line.
- **Restart required**: like every other dynamic-cap knob, `maxConcurrent` is
  captured once at daemon **startup**, not re-read per tick — a config edit takes
  effect on the *next* restart
  (`./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh`),
  not live.

#### Per-tick admission (ramp) cap (#4234)

`resolve_dynamic_max_concurrent` is a **live** ceiling, recomputed every tick,
so it can *jump* tick-to-tick — e.g. several exhausted token accounts resetting
at once raises the token axis from ~2 to ~14, or an operator raises
`maxConcurrent` on a live host. Before #4234, a jump like that let a **single
tick** admit every newly-eligible candidate up to the new, larger cap in one
shot. Host load is a **lagging** signal: a burst admitted together all ramp
their work minutes later, well after the tick that "safely" admitted them
observed a still-quiet host. This is the exact failure mode of the #4231
incident's second wave — the host re-spiked at 01:41 after load had already
dropped to 8, because the admission decision had already been made by the time
load caught up. (Since #4512 the heavy stages additionally serialize on the
[build slot](#machine-wide-build-slot-4512), so a burst's *builds* queue even
when its admissions do not; the ramp cap remains the smoother for everything
else a burst does.)

`autonomous.workFinder.maxAdmissionsPerTick` (`LOOM_WORK_FINDER_MAX_ADMISSIONS_PER_TICK`,
default **3**, mirroring `maxConcurrent`'s default) bounds how many **new**
sweeps a single tick may admit, **independent of** `max_concurrent` — the two
caps are separate and both apply (`tick_report.deferred_capacity` counts
candidates deferred by the concurrency ceiling, `tick_report.deferred_ramp_cap`
counts candidates deferred by this ramp cap; either alone can defer a
candidate, and both fire in the same tick when both bind). A large cap jump
therefore ramps up over several ticks instead of bursting in one — each
subsequent tick re-samples CPU/disk/token headroom fresh, so a ramp that turns
out to be too aggressive self-corrects within one interval (default 60s)
rather than in one uncontrolled burst. Resolved with the standard precedence
**env > config > default**, single-root, at daemon startup — the same
startup-capture pattern as `maxConcurrent`: the ramp
cap's whole purpose is to smooth admission *within* the live per-tick
re-computation of `max_concurrent`, so the knob itself does not need to be
live; retuning it takes effect on the next daemon restart.

#### `dispatch_sweep` headroom advisory (#4234)

Before #4234, the dynamic cap above only gated the autonomous work finder's
**own** dispatches — the `dispatch_sweep` IPC/MCP handler (the entry point for
`mcp__loom__dispatch_sweep`, `loom-daemon dispatch`, and any other
operator-driven fan-out) dispatched directly with **no** headroom consult at
all. Any operator- or MCP-driven `dispatch_sweep` call was completely ungated;
the #4231 6-way fan-out that triggered the host meltdown was dispatched through
exactly this handler.

The fix is **advisory-first**, matching `capacity.rs`'s token-backpressure
"never a halt" precedent (#3902) rather than adding a hard-defer/`force`
protocol change: `dispatch_sweep` **always dispatches** — an explicit
operator/MCP request is a deliberate act, and the work finder's own dynamic
cap remains the hard backstop for *its own* autonomous dispatches — but now
computes the same `resolve_dynamic_max_concurrent` headroom the work finder
uses (token/disk/configured-max, scoped to the target repo) and, on a
**state change** into/out of "occupancy at or over that headroom" for that
repo, logs a warning and publishes a `daemon.dispatch.headroom_advisory`
event-bus event (state-change-deduped — never a per-call stream, keyed
per-repo so a multi-workspace daemon's repos don't cross-suppress each
other's advisories). This required **zero protocol change**: no new
`Request::DispatchSweep` field, no new `Response` variant — the advisory is a
side channel exactly like the token-capacity advisory. Every `dispatch_sweep`
call's log line additionally always names the current occupancy/dynamic-cap
axes (not just on the deduped transition), so `RUST_LOG=loom_daemon=info`
shows the same headroom picture the work finder's own tick log shows, without
duplicating the `disk_headroom_limit` plumbing `loom-daemon status`
(`build_daemon_status`) already exposes.

Nothing in this handler may block, because it holds the sweep-registry mutex
from the assessment through the dispatch call. Since #5270 the headroom is
`min(disk, ram, configured max)` — cheap filesystem/config reads (plus a
non-sleeping `/proc/meminfo` read or a flag-less `vm_stat` snapshot on macOS
for RAM), no CPU sampling at all. (Before #4512 this had to carefully avoid
the **refreshing** CPU probe, whose macOS `iostat` refresh sleeps ~1s and
would have stalled every other IPC request on the same registry for that
second; removing the CPU term removed that hazard outright, and RAM headroom
deliberately preserves the same non-blocking contract.)

**Per-token concurrency factor (#3947, retired from the cap by #5270, removed
entirely by #5743).** The token axis used to be `healthy × factor`, not
`healthy × 1` — the factor resolved with the standard precedence **env
(`LOOM_PER_TOKEN_CONCURRENCY`) > config (`autonomous.perTokenConcurrency`) >
default (2)**. Since #5270 the token axis no longer participated in
`dynamic_cap` at all, leaving the factor purely informational; `#5743` finished
the retirement by deleting the knob, its env var, and the derived `healthy N ×
per-token M = P` line from `loom-daemon status` / `calibrate` entirely — that
line disclaimed itself as "informational only" but was still misread as a live
cap term more than once. `= min(...)` is now simply `= min(disk headroom 120,
ram headroom 40, configured max 3)`, with no token term at all. A config file
that still sets `autonomous.perTokenConcurrency` parses fine (unknown keys are
ignored, not an error) — the key just does nothing. A separate line reports the
live host CPU **observation** — explicitly labelled as not a cap term, e.g.
`host cpu (observed, not a cap term since #4512): 28 logical cores, 85% idle
measured (≈4.2 cores consumed), 1m loadavg 6.10`, degrading to a `1m loadavg …
(no idle sample yet)` or `no CPU signal on this platform` line when the
measurement is unavailable (#3978 AC4; measured-idle signal #4031; demoted to
observation in #4512) — and, since #5270, the `admission_brake` block reports
whether that same load reading is currently *holding* new admissions (the
brake, not this observation line, is what gates CPU today).

**"Currently binding" vs "smallest ceiling" (#4031).** The dynamic cap is the
*minimum* of several ceilings, but a ceiling only *binds* once in-flight
occupancy reaches it. Below the cap the limiter is simply how much ready work
exists, not any resource term — so the status view gates its bottleneck
diagnosis on real occupancy (`in_flight.len() >= dynamic_cap`, surfaced as the
`capacity_bound` field, `#[serde(default)]`). With in-flight below the cap it
prints `not capacity-bound (N in flight, cap M — the limiter is work
availability, not disk/RAM/CPU)`; at or above the cap it names the binding
term as before. The `= min(…)` breakdown line is untouched — those genuinely
are ceilings. The JSON status carries the same `capacity_bound` boolean so
scripted consumers aren't misled at low occupancy either.

**Token-axis probe/daemon disagreement can no longer skew the cap (#4344,
superseded by #5270; re-scoped, not deleted, by #5305).** `resolve_capacity`
still prefers a fresh client-side `loom-daemon tokens check --json` probe over
the daemon's own ranking read for the *reported* healthy/total/exhausted
account counts (useful, current numbers for the account-health line), and
since #5270 it no longer recomputes its own `effective_cap` from those counts
— that is always exactly the daemon's own `report.dynamic_cap`. The old
`#4344` incident (dispatch pinned at a token term of `0 × per-token = 0` for
~40 minutes because the daemon's ranking read had diverged from a fresher
probe) is structurally impossible for the *cap*: a stale or diverged
`.ranking` can misinform account **selection**, but it can never again pin
`dynamic_cap` to 0. `token_bound` itself, however, is **not** hardcoded
`false` — PR #5304 over-removed it that way (a #5305 fix), permanently
dead-ending the guidance branch below even with zero healthy accounts.
`token_bound` now means genuine starvation (zero healthy accounts), decoupled
from the cap: `loom-daemon status` prints `token-bound: NO healthy accounts —
the concurrency cap is unaffected (#5270), but every spawn will fail account
selection…` whenever it holds. The ranking-divergence headline is likewise
re-scoped rather than retired — renamed `⚠ SPAWN SELECTION IS TOKEN-STARVED: …`
(from the historical `⚠ DISPATCH IS TOKEN-STARVED: …`) to reflect that only
account **selection**, not the cap, is at stake when the daemon's own
`.ranking` read shows 0 healthy while a fresher probe/ranking disagrees. The
root fix for `.ranking` divergence itself — unifying which file
[`capacity::read_ranking`] consults — remains in place; see the healthy-token
row of the "retired" input table above. See also #5269/PR #5283's per-repo
ranking-staleness surfacing on `loom-daemon health` — a separate, `health`-side
mechanism this `status`-side headline must not contradict.

**Session-limit fault handling (#3947).** Stacking can occasionally trip a
**concurrent-session-limit** fault on a token (the account is healthy but cannot
start another *simultaneous* session right now). This is a **capacity** signal,
not quota exhaustion, so `classify-error.sh` classifies it distinctly as
`SESSION_LIMIT` (checked *before* `TOKEN_EXHAUSTED` so the "session limit" wording
is not swallowed by the exhaustion regex). `claude-wrapper.sh` responds by
re-selecting a **different** account and retrying **without** appending the
current token to `.bad_tokens` — a capacity fault must never poison the healthy
pool. Re-selection advances the rotation cursor so a healthy sibling is preferred,
which backs off stacking for the saturated account; a bounded
`LOOM_MAX_SESSION_LIMIT_RETRIES` (default 10) guards a fully-saturated pool from
spinning, after which it falls through to normal transient backoff (still without
marking the token bad).

**Per-model limit handling + one retry verdict (#4501).** The CLI also emits a
**per-model** ceiling — `You've reached your Fable 5 limit. Run /usage-credits to
continue or switch models with /model.` — whose model name sits between "your" and
"limit". That phrasing matched none of the older "hit your … limit" patterns, so
rotation never fired and every child died instantly; `classify-error.sh` now
matches the `reached your <model> limit` family as `TOKEN_EXHAUSTED` (ordered
*after* the `SESSION_LIMIT` branch so "reached your concurrent session limit`"
keeps its capacity classification). Reusing `TOKEN_EXHAUSTED` is a deliberate safe
over-approximation: a Fable-only ceiling does not exhaust `sonnet` on the same
account, so the pool is slightly pessimistic but rotation is correct, and no
downstream consumer has to learn a new category. Relatedly, `claude-wrapper.sh`'s
retry decision no longer keeps its own pattern list — `is_transient_error` derives
it from `classify_error`'s category via the shared `classification_is_transient`
**deny-list** (terminal: `SUCCESS`, `TIMEOUT`, `TOKEN_EXPIRED`, `CWD_DELETED`,
`MODEL_REFUSAL`, `FATAL`; everything else retried, bounded by `LOOM_MAX_RETRIES`).
That makes the observed contradiction — `Non-transient error detected - not
retrying` printed next to `classification=RECOVERABLE` — structurally impossible,
and means an *unrecognized* non-zero exit is now retried rather than dying on
attempt 1.

**Per-model-tier credit exhaustion (#5687).** A *fourth* account-side signature —
`You're out of usage credits` — matched none of the patterns above and fell
through to the `RECOVERABLE` catch-all, so the wrapper retried the same model
with backoff instead of rotating. It is now classified `MODEL_CREDITS_EXHAUSTED`,
ordered **after** `TOKEN_EXHAUSTED` so #4501's `reached your <model> limit`
message (which also mentions `/usage-credits`) keeps its existing category. Unlike
#4501 this one is a **distinct category rather than another `TOKEN_EXHAUSTED`
phrase**, because the remedy differs in kind: credits are scoped to a model tier,
so the same account on a cheaper model still works. That matters on the in-session
`/loom:sweep` Task-dispatch path, which has no token pool to rotate through but
picks a `model` at every dispatch — see `sweep.md` → "Credit-exhaustion fallback"
for the one-rung-down recovery, backed by `resolve-model.sh --downgrade`
(`fable → opus → sonnet → haiku`, exit 3 at the cheapest rung).
For the daemon/wrapper path the new category is a pure **rename**:
`is_account_exhaustion` accepts it alongside `TOKEN_EXHAUSTED` (rotate + mark
bad), `classification_is_transient` keeps it retryable, and
`tokens_pool::health` records the identical `PlanExhausted` reason and cooldown.
The pool has no per-model account state, so it must stay that way — the distinct
name exists for the orchestrator's remedy choice and for forensics, not for a
different pool policy.

**Monthly spend-limit kill (#5631/#6518).** "You've hit your monthly spend
limit" was already widened into the `TOKEN_EXHAUSTED` regex by #5631 — on the
wrapper's rotation path it rotates correctly today with no further change. The
**reaper's** own signature table had silently drifted out of that lockstep,
though: `sweep_registry::crash_signals::exhaustion_signatures` still carried the
pre-#5631 fixed alternation `hit your (limit|session limit|weekly limit)`, which
does not match a *spend* limit (no `usage` token either, so the `monthly usage
limit` arm misses it too). A spend-capped detached child therefore matched no
exhaustion signature at insta-crash classification time and its death was
charged to the **issue's** quarantine tally instead of the account — the same
backwards attribution #4501 fixed for the per-model ceiling. #6518 restores the
bounded-filler shape (`hit your (?:\S+\s+){0,3}limit`) so the reaper labels it
`account-exhausted:rate-limited`, exactly as the wrapper already treated it. The
pool's *treatment* is unchanged (transient exhaustion); only the attribution was
wrong.

The other half of #6518 is architectural, not a missing
pattern: it is the same gap #5687 solved for credit exhaustion — the in-session
`/loom:sweep` Task-dispatch path has no subprocess to run `classify_error`
through at all, so the orchestrator pattern-matches the raw failure text itself
(`sweep.md` → "Spend-limit fallback"). Its remedy differs from the
credit-exhaustion ladder-walk: a spend cap's scope (whole account vs. one model
tier) is not knowable from the signature the way credit exhaustion is, so the
first-line in-session remedy is re-dispatching the same attempt with the
`model` param **omitted** (inheriting the session default) rather than
`resolve-model.sh --downgrade`; only once that is exhausted (or was never
available — the killed dispatch already had no `model` override) does it fall
through to the ordinary mid-phase-death recovery. No new `classify-error.sh`
category was added for this — the existing `TOKEN_EXHAUSTED` classification is
correct and sufficient for every path that actually calls the classifier.

**Auth-dead (401 invalid-bearer-token) rotation, distinct from exhaustion
(#6030).** A wave of daemon-dispatched children died within minutes ending in
`Failed to authenticate. API Error: 401 Invalid bearer token`. This is a
different failure class from every pattern above: an exhausted account
recovers on its own once its quota window resets, but an auth-dead one (a
revoked/invalid OAuth token) fails **every** dispatch forever until a human
re-authenticates it. The phrase matched no `classify_error` category, so it
fell through to the `RECOVERABLE` catch-all — the wrapper retried the same
dead credential with backoff until `MAX_RETRIES`, then died without ever
marking the account bad, so the next spawn could pick the exact same
auth-dead account again. `classify_error` now matches `invalid bearer token`
(alongside the existing `401 … authentication_error` / `token has expired`
patterns) as `TOKEN_EXPIRED`, and a new `is_account_auth_dead` /
`rotate_auth_dead_account` pair in `claude-wrapper.sh` — structurally
parallel to `is_account_exhaustion` / `rotate_exhausted_account` — marks the
account bad with an `"auth-dead: ..."` reason (not `"exhausted: ..."`) before
rotating, sharing the same `rotations`/`max_rotations` cap. `bad_tokens`'s
existing `auth_reason_regex` already classifies any reason string containing
`auth` as `BadReasonClass::Auth` (permanent, clears only via `tokens
unblock`), so no format change was needed there — only the wrapper-side gap
(the account was never marked bad in the first place) needed closing. `tokens
check`'s probe path (`discover_tokens`) also switched from a naive
whole-line-equality `.bad_tokens` check to the same `blocking_entry_in_dir`
parser `select.rs` uses, so a bad-marked account's real class/reason (e.g.
`auth: auth-dead: 401 Invalid bearer token`) now surfaces in the table/JSON
output instead of the opaque `bad_token_listed` — telling an operator whether
an account needs `claude login` + `tokens unblock` or will simply clear on
its own cooldown.

The **effective** per-tick concurrency is then `min(dynamic_cap, backlog_depth)`:
`tick()` iterates the ready `loom:issue` rows and stops at the cap, so
concurrency **scales up** as the backlog grows and drains to **zero** dispatches
when the queue is empty (no capacity is pre-reserved and no idle workers are
spawned). A token pool of 0 (rotation not bootstrapped) yields a cap of 0 —
the finder dispatches nothing, matching `spawn-claude.sh`'s `EX_CONFIG`
hard-fail on a missing pool. The `df` probe runs once per tick and is negligible
on the 60s default interval. Bad-token-aware pool counting (subtracting
`.bad_tokens` entries) is a tracked follow-up; the first pass counts `*.token`
files.

The loop is **idempotent** (an issue already in the registry is never
re-dispatched) and **fail-safe**: a forge-query error aborts only that tick
(logged, retried next tick) and a single dispatch error is logged and counted,
never crashing the daemon. Dispatches surface on the frozen
`sweep.global.dispatch` topic (emitted inside `dispatch()`); the finder adds no
new event topics.

Enable it with `LOOM_WORK_FINDER=1` (unset/false-y = OFF) **or** from committed
config (`autonomous.workFinder.enabled`, see "Operability" below). Tunables:
`LOOM_WORK_FINDER_INTERVAL_SECS` (default 60 — tighter than the epic
supervisor's 300s so the `loom:issue` backlog drains promptly),
`LOOM_WORK_FINDER_MAX_CONCURRENT` (default 3 — the operator **ceiling** in the
dynamic policy above, not a fixed target), and `LOOM_PER_WORKTREE_GB` (default
2 — the per-worktree disk estimate the disk-headroom bound divides by). A zero
or unparseable value for any of these falls back to its default.

> **Scope note**: the work finder dispatches **already-approved** `loom:issue`
> items; it does **not** generate new work itself — it only closes the gap
> between an approved issue and its build. The one adjacent exception is the
> role runner's `onIdle` edge, which the work finder *observes* for it: a repo
> that opts in with `onIdle: ["architect"]` gets a capped proposal pass when its
> backlog empties (#5656, see [Idle-addressable-only roles](#idle-addressable-only-roles-architect-5656)).
> Interval-cadence Architect/Hermit work generation remains out of scope
> (follow-up #3381).

**Occupancy: startup-proof grace, distinct from the startup watchdog (#4003).**
A dispatch slot is checked out (counted as occupied) the instant
`SweepRegistry::dispatch()` returns — i.e. at `fork/exec` success, before the
child has proven it reached the API, created a worktree, or wrote a
checkpoint. Before #4003 that slot stayed occupied for the sweep's entire
`Running`/`Pending` lifetime regardless of whether the child ever showed a
sign of life, so a child wedged at startup (e.g. a hung MCP-init) held its
slot for the full 300s startup-watchdog window
(`sweep_registry::DEFAULT_WATCHDOG_TIMEOUT_SECS`) before anything reclaimed
it.

`SweepRegistry::occupied_issues()` (consumed by `RegistryDispatcher::occupancy`,
which `tick`/`tick_multi` now use to seed occupancy instead of
`in_flight().len()`) narrows that: a sweep still counts while it is inside its
**startup-proof grace window** (`elapsed < grace` — a fresh dispatch legitimately
has produced nothing yet) *or* once it has proven startup progress. Progress
reuses the exact signal the startup watchdog already polls
(`sweep_made_progress` — a worktree at `.loom/worktrees/issue-<N>`, a checkpoint
at `.loom/sweep-checkpoint/issue-<N>.json`, or log output past the spawn header,
see `log_has_progress`) and latches through the SAME per-`SweepId`
`watchdog_progressed` set the watchdog itself maintains, so a signal observed by
either mechanism is remembered by both. A sweep older than `grace` with **zero**
proven signal is excluded from occupancy — freeing its slot for a healthy
queued sweep — while the sweep's own `SweepState` stays `Running` untouched:
this is occupancy accounting only, never a cancel/re-dispatch action. (De-dup
safety — never double-dispatching the SAME issue — still comes from
`in_flight()`, the registry's claim lock, and the forge `loom:building` label,
none of which this change touches.)

The grace window (default **90s**, `DEFAULT_STARTUP_PROOF_GRACE_SECS`) is
deliberately much shorter than the 300s watchdog timeout: the watchdog's 300s is
sized to the measured 110–150s dispatch→**worktree** latency under concurrency
(#4088), a late, heavy signal, while `log_has_progress` fires far earlier — the
instant Claude Code itself produces any output past the spawn header, typically
within seconds even under contention. Tunable per workspace with the standard
precedence **env (`LOOM_SWEEP_STARTUP_PROOF_GRACE_SECS`) > config
(`autonomous.watchdog.startupProofGraceSecs`) > default (90)**, resolved the same
way as `dispatch_stagger` (`SweepRegistry::set_startup_proof_grace`, set once per
workspace at provision time by `main.rs` / `WorkspacePool::get_or_provision`).
`SweepRegistry::unproven_startups()` is a read-only diagnostic (no latch
mutation from the read itself; observes the same latch) returning
`(issue, time_since_dispatch)` for every live sweep that has not yet proven
startup — for status/observability, not for gating.

### Token-capacity backpressure (#3902; token axis retired from the cap by #5270)

At scale, rotation accounts used to hit their 5h/7d rate limits and go
`exhausted`, and dispatching to an exhausted account produces startup hangs /
mid-build deaths, so the finder treats a genuine token limit as a **capacity
signal** — alert, recover — non-blocking (the "slow down" half below is
historical: #5270 removed the token axis from the dynamic cap itself, so
account health can no longer *slow dispatch down*, only warn about it):

1. **Slow down (backpressure) — historical, superseded by #5270.** The token
   axis used to be a term in `dynamic_cap`: the count of **healthy**
   (`available`) accounts read from `.loom/tokens/.ranking`
   (`capacity::token_axis_limit`), not the flat `*.token` count — when
   accounts went exhausted the cap backed off toward the healthy count, down
   to 0 when every account was exhausted. Operator direction on #5270 ("we
   should only ever limit parallelism based on the machine disk/RAM/CPU")
   removed this backoff entirely: `.ranking` health now drives spawn-time
   **selection** only (prefer fresher/healthier accounts, skip
   `blocked`/revoked ones), never admission. `capacity::token_axis_limit`
   still exists and is still reported (see the "retired" input table in the
   dynamic-cap section above), but its output no longer bounds anything.
2. **Alert (advisory only, unchanged posture).** The pressure advisory
   (`capacity::assess_pressure`) still runs every tick. Its `token_bound`
   predicate is **not** a cross-axis "is tokens the (co-)minimum of
   disk/RAM/ceiling" comparison — PR #5304 briefly left it as one
   (`token_limit <= disk.min(ram) && token_limit <= configured_max`), which
   false-positived the advisory whenever *any other* axis deferred work while
   the pool still had healthy accounts left (a #5305 fix). It is now a pure
   starvation check: `deferred > 0 && token_limit == 0` — zero healthy
   accounts, full stop, regardless of which axis actually deferred the tick's
   work. It still exists purely to prompt an operator to add capacity when
   the pool is genuinely exhausted and a backlog is building — it is
   deliberately **not** what gates dispatch (#5270 dropped that role
   entirely). On the state
   change into a pressured reading it emits an add-capacity advisory naming
   concrete levers — add accounts to `~/.claude-monitor/accounts.env` +
   `loom-daemon tokens bootstrap`, or buy API credits, then re-probe with
   `loom-daemon tokens check --ranking` — with the current numbers (queued
   count, healthy/total accounts, exhausted count, estimated drain time at
   current capacity). If accounts are `blocked` on revoked tokens, `bootstrap
   --force` cannot recover — the advisory also names `loom-daemon tokens
   import-from-monitor --force && loom-daemon tokens check --ranking` as the
   recovery lever for that case. The advisory surfaces on **three** channels:
   the daemon log (`warn`), the `daemon.capacity.advisory` event-bus topic,
   and the `capacity` section of `loom-daemon status`. It is
   **deduplicated** — one advisory on entry, one recovery on exit, never
   per-tick spam. Advisory only; it never blocks dispatch (and, since #5270,
   nothing about account health ever did).
3. **Recover.** The finder re-reads the ranking every tick (bounded cadence =
   the tick interval), so as accounts reset to `available` the advisory
   clears and the queued `loom:issue` backlog continues to drain at whatever
   rate `min(disk, ram, configured_max)` allows — no manual intervention. A
   symmetric recovery line/event fires on the way out of the pressured state.

The `estimated_drain_minutes` figure is a coarse `ceil(queued / healthy) ×
NOMINAL_SWEEP_MINUTES` (30 min nominal) aid, not a precise SLA — the daemon does
not track live per-sweep durations here. Near-ceiling granularity is limited to
the `.ranking` discrete status word (`exhausted` is already ≥ 0.95 utilization);
a finer sub-exhausted (≥ 0.90) bucket would read the richer `loom-daemon tokens check
--json` utilization and is a tracked follow-up. Even rotation/staggering of
dispatches across the available account set (so 5h/7d windows reset in a
staggered pattern) lives in the spawn-time selector (native `loom-daemon tokens
select`, `loom-daemon/src/tokens_pool/select.rs` — cut over from
`loom_tools.tokens.select` in #4228), not the daemon's own dispatch loop, and is
a separate follow-up.

## Operability — config, start/stop, E2E (Phase D, #3813)

> **Machine-level `loom` dispatcher (Epic #3835 Phase 3a #4157, Phase 3b #4229).**
> A machine-level `loom` entry point at `~/.local/bin/loom` (sibling of
> `~/.local/bin/loom-daemon`) resolves the `~/.local/share/loom` checkout and
> exec's into it — `loom start|stop|restart|status|sweep|update`. It is distinct
> from the per-repo tmux agent-pool manager `./.loom/bin/loom`; the
> name-collision resolution, checkout layout, and the thin-`update` boundary
> are documented in [`machine-dispatcher.md`](machine-dispatcher.md). Every
> delegating verb hands its lifecycle-script delegate
> (`loom-daemon-start.sh`/`-stop.sh`/`-update.sh`) the resolved checkout via
> `LOOM_MACHINE_CHECKOUT`, so the plist `WorkingDirectory` and the
> `.daemon.pid`/`.daemon.flags`/startup-log home resolve consistently —
> `$HOME/.loom`, not a `$PWD`-derived repo — regardless of which directory
> `loom` was invoked from (#4229). Direct invocation of a lifecycle script (no
> dispatcher) is unaffected: the pre-#4229 `$PWD`-based `find_repo_root()`
> contract remains the fallback, unchanged.

Phases A–C built the autonomous *engine* (work finder, dynamic concurrency,
main-health gate) as env-var-only surfaces. Phase D (#3813) adds the
operator-facing layer: a committed config surface, safe start/stop wrappers for
the raw daemon process, and a documented end-to-end acceptance playbook.

### Config surface (`.loom/config.json → autonomous`)

Autonomous mode can be enabled and tuned entirely from committed config — no env
vars required — so a repo can declare "this workspace runs autonomous mode with
concurrency ceiling 5" and share it with the team:

```json
{
  "autonomous": {
    "model": "sonnet",
    "workFinder": {
      "enabled": true,
      "intervalSecs": 60,
      "maxConcurrent": 5,
      "maxAdmissionsPerTick": 3,
      "quarantine": {
        "enabled": true,
        "threshold": 3,
        "ttlSecs": 3600,
        "instaCrashSecs": 60
      },
      "dispatchBackoff": {
        "enabled": true,
        "baseSecs": 60,
        "maxSecs": 900
      },
      "noopCooldown": {
        "enabled": true,
        "cooldownSecs": 3600
      },
      "extraSkipLabels": ["blocked-upstream"]
    },
    "hostBreaker": {
      "enabled": true,
      "loadPerCoreTrip": 2.5,
      "sustainTicks": 3,
      "cooldownSecs": 300
    },
    "rateLimitBreaker": {
      "enabled": true,
      "fallbackCooldownSecs": 900
    },
    "mainHealthGate": {
      "enabled": true
    },
    "roleRunner": {
      "enabled": true,
      "roles": ["champion", "curator", "judge", "doctor", "auditor", "guide", "hermit"],
      "intervalSecs": 300,
      "onIdle": ["champion"]
    },
    "watchMonitor": {
      "enabled": true,
      "intervalSecs": 120,
      "expirySecs": 86400
    },
    "heartbeat": {
      "enabled": true,
      "intervalSecs": 60
    },
    "dispatchStaggerMs": 2000,
    "watchdog": {
      "enabled": true,
      "timeoutSecs": 300,
      "intervalSecs": 30,
      "reviewStall": true,
      "reviewStallTimeoutSecs": 2700
    }
  }
}
```

**Precedence is `env var > config value > built-in default` for every knob.** An
operator env var still overrides the committed config for a single run
(`LOOM_WORK_FINDER=0 loom-daemon` disables the loop even if config enables it).
An **absent `autonomous` block is byte-for-byte identical to the pre-#3813
env-only behavior** — the config read soft-fails (missing file / malformed JSON /
missing block all resolve to "no config value → fall through to env/default"),
exactly like `main_health_gate::read_build_gate_config`.

**Not every knob below is live.** Some of this block is re-read from
`.loom/config.json` on the next tick of whichever loop owns it (the whole
`autonomous.roleRunner.*` sub-block is, per registered root) — but many are
resolved exactly once, during daemon bring-up, and then held for the rest of
that process's life as a frozen value or a process-global handle. Editing one
of those and merely landing the edit on disk changes nothing on a host until
the daemon is **restarted** — the table below marks each with
**"restart required"**; see
[`fleet-config-lifecycle.md`](fleet-config-lifecycle.md) for the "landed !=
effective" convention this exists to support and the mechanical test for
knobs not yet audited here.

| Config key | Env override | Default | Notes |
|------------|--------------|---------|-------|
| `autonomous.model` | *(per-dispatch `dispatch_sweep` `model` param)* | `sonnet` | Model pinned on **every** daemon-dispatched child (work-finder, epic supervisor, and `dispatch_sweep` when its `model` param is absent). See below (#3944) |
| `autonomous.workFinder.enabled` | `LOOM_WORK_FINDER` | `false` | Master on/off for the finder loop. **Restart required** — read once, before the loop is spawned; flipping it in config alone does not start/stop an already-running daemon's loop (#5963) |
| `autonomous.workFinder.intervalSecs` | `LOOM_WORK_FINDER_INTERVAL_SECS` | `60` | Zero/invalid → default |
| `autonomous.workFinder.maxConcurrent` | `LOOM_WORK_FINDER_MAX_CONCURRENT` | `3` | The per-machine **sweep-dispatch** admission knob since #4512 — **it bounds sweeps only; role-runner agents are admitted outside it and carry their own `autonomous.roleRunner.maxConcurrent` ceiling (#6102)** — an operator ceiling, not a fixed target, tuned empirically from `loom-daemon calibrate` / `status`. Per-machine **and workload-dependent** (#4903): ~10+ on an 8-core API-bound (software) worker, but **2–3** on the same 8 cores running analog/simulation sweeps. **Restart required** — `resolve_max_concurrent_with_config` runs once during bring-up and the resulting `configured_max` is threaded into the loop as a frozen value; the per-tick `dynamic_cap` recomputes only its `disk`/`ram` headroom terms around that fixed operator ceiling, so retuning this key in config alone changes nothing until the daemon restarts (#5963). The `work_finder: enabled (multi-workspace, …)` startup log line names the resolved value and which layer supplied it — `source=env`/`config`/`default` (#6203) — so an operator can confirm a config edit will actually take effect on the next restart without waiting for a tick. See [Sizing `maxConcurrent`](#sizing-maxconcurrent-per-machine-and-per-workload-4512-4903) below |
| `autonomous.workFinder.maxAdmissionsPerTick` | `LOOM_WORK_FINDER_MAX_ADMISSIONS_PER_TICK` | `3` | Per-tick **ramp** cap (#4234) — bounds how many *new* sweeps one tick may admit, independent of `maxConcurrent`/the dynamic cap. Zero/invalid → default; resolved once at startup, the same startup-capture pattern as `maxConcurrent`. **Restart required** to pick up a change (#5963) |
| `autonomous.workFinder.saturationBrake.enabled` | `LOOM_ADMISSION_BRAKE` | `true` | Saturation admission brake on/off (#4903). A safety backstop — **defaults on**. Holds *new* admissions while the host is already saturated; never preempts a running sweep. Env truthy (`1`/`true`/`yes`/`on`) enables, any other value disables; wins over config. **Restart required** — resolved once at startup and registered as a process-global handle alongside the host breaker (#5963). See [Saturation admission brake](#saturation-admission-brake-4903) below |
| `autonomous.workFinder.saturationBrake.loadPerCoreHold` | `LOOM_ADMISSION_BRAKE_LOAD_PER_CORE` | `0.95` (`4.0` before #5270) | Load-per-core at/over which new admissions are held for that tick. `<= 0`/invalid → default. Since #5270 sits deliberately *below* the host breaker's `2.5` trip: the brake is now the primary "dumb mode" CPU gate and engages first (a single over-threshold reading), the breaker remains the slower sustained-distress trip. **Restart required** — same startup-resolved global as `enabled` above (#5963) |
| `autonomous.workFinder.saturationBrake.starvationWarnSecs` | `LOOM_ADMISSION_BRAKE_STARVATION_WARN_SECS` | `300` | Seconds of continuous held+0-in-flight before the `WARN`-level `STARVING` log fires once per streak (#5715). `<= 0`/invalid → default. See [Starvation escape hatch](#starvation-escape-hatch-5715) |
| `autonomous.workFinder.saturationBrake.starvationEscapeSecs` | `LOOM_ADMISSION_BRAKE_STARVATION_ESCAPE_SECS` | `900` | Seconds of continuous held+0-in-flight before the escape hatch yields one tick despite the raw load still being over threshold, logged at `ERROR` (#5715). `<= 0`/invalid → default |
| `autonomous.workFinder.quarantine.enabled` | `LOOM_WORK_FINDER_QUARANTINE` | `true` | Insta-crash quarantine on/off (#3939). A safety backstop — defaults on |
| `autonomous.workFinder.quarantine.threshold` | `LOOM_WORK_FINDER_QUARANTINE_THRESHOLD` | `3` | Consecutive insta-crashes before an issue is quarantined. Zero/invalid → default |
| `autonomous.workFinder.quarantine.ttlSecs` | `LOOM_WORK_FINDER_QUARANTINE_TTL_SECS` | `3600` | How long a quarantine entry persists before auto-release. Zero/invalid → default |
| `autonomous.workFinder.quarantine.instaCrashSecs` | `LOOM_WORK_FINDER_QUARANTINE_INSTA_CRASH_SECS` | `60` | Checkpoint-less death within this window of dispatch counts as an insta-crash. Zero/invalid → default |
| `autonomous.workFinder.dispatchBackoff.enabled` | `LOOM_DISPATCH_BACKOFF` | `true` | Per-issue dispatch backoff on/off (#4485). A safety backstop — defaults on. Env truthy (`1`/`true`/`yes`/`on`) enables, any other value disables; wins over config |
| `autonomous.workFinder.dispatchBackoff.baseSecs` | `LOOM_DISPATCH_BACKOFF_BASE_SECS` | `60` | Backoff applied after the **first** failed dispatch of an issue; doubles per consecutive failure. Zero/invalid → default |
| `autonomous.workFinder.dispatchBackoff.maxSecs` | `LOOM_DISPATCH_BACKOFF_MAX_SECS` | `900` | Ceiling on the doubling — also the idle window after which an issue's consecutive-failure tally restarts at zero. Zero/invalid → default; clamped up to `baseSecs` |
| `autonomous.workFinder.noopCooldown.enabled` | `LOOM_WORK_FINDER_NOOP_COOLDOWN` | `true` | No-op re-dispatch cooldown on/off (#6670). A safety backstop — defaults on. Env truthy (`1`/`true`/`yes`/`on`) enables, any other value disables; wins over config |
| `autonomous.workFinder.noopCooldown.cooldownSecs` | `LOOM_WORK_FINDER_NOOP_COOLDOWN_SECS` | `3600` | How long a self-reported no-op release (`loom-daemon noop-cooldown record` / `RecordNoopRelease`) holds an issue out of dispatch. Flat, non-exponential — every record re-arms the same window. Zero/invalid → default. `/loom:sweep`'s Builder phase is the first in-repo caller via `defaults/scripts/record-noop-release.sh` (#6740) — see "No-op re-dispatch cooldown (#6670)" below |
| `autonomous.workFinder.extraSkipLabels` | `LOOM_WORK_FINDER_EXTRA_SKIP_LABELS` (comma-separated) | `[]` | Per-workspace/per-repo **additional** label names (#6685) the work-finder treats as a skip/park signal, beyond the hardcoded `loom:blocked` / `loom:operator-only` (`PARK_LABELS`) — e.g. a repo-local `blocked-upstream` label that will never be renamed to a `loom:*` name. Purely additive to `SKIP_LABELS`' candidate-query filter (`WorkItem::is_skipped_with_extra`); it does **not** extend the separate dispatch()-level park-label guard (#4444) above, which stays keyed on `PARK_LABELS` only. Env replaces config entirely when set (even to an empty string); resolved once per workspace, live on the next tick (a cheap `.loom/config.json` read, no daemon restart needed). **`loom:building` can never be added to the resolved list** — filtered out defensively even if named explicitly in config/env, so a misconfiguration can never re-introduce the "an in-flight claim is treated as a park" regression `SKIP_LABELS`' own doc comment warns against |
| *(env only)* | `LOOM_OPEN_PR_MEMO` | `true` | Verified-open-PR memo for the #4123 open-PR dispatch guard (#6788). Falsy (`0`/`false`/`no`/`off`) disables; anything else (including unset) enables. When on, the guard (a) reuses a verified "issue #N has open linked PR #M" answer for 15 minutes instead of re-running the closes-graph query on every work-finder tick, and (b) when **both** the GraphQL probe and its #5911 REST fallback fail, re-verifies that one known PR over a single `GET repos/{owner}/{repo}/pulls/{M}` before conceding. The documented fail-open contract is unchanged: with no memo, or if that recheck also cannot answer, the guard still proceeds. In-memory only — a daemon restart clears it. Disable only to restore the exact pre-#6788 probe |
| *(env only)* | `LOOM_EMPTY_POOL_BREAKER_THRESHOLD` | `3` | How many **distinct** issues must die in `spawn-claude.sh`'s token-selection step (exit 78) inside the window below before new dispatch to that workspace is paused (#6614). Distinct *issues*, not raw failures: one issue cycling through its own `dispatchBackoff` can never trip it. Crossing it trips the existing pre-flight advisory (#4386) + half-open dispatch gate (#5030) — one loud `ERROR` plus a `daemon.preflight.advisory` event — and the first dispatch that gets past token selection clears it. Zero/invalid → default |
| *(env only)* | `LOOM_EMPTY_POOL_BREAKER_WINDOW_SECS` | `1800` | Trailing window over which those distinct token-selection deaths are counted (#6614) — twice the `dispatchBackoff.maxSecs` plateau, so a systemic fault always accumulates while isolated failures spaced further apart never do. Zero/invalid → default |
| `autonomous.hostBreaker.enabled` | `LOOM_HOST_BREAKER` | `true` | Host-distress circuit breaker on/off (#4235). A safety backstop — **defaults on**. Env truthy (`1`/`true`/`yes`/`on`) enables, any other value disables; wins over config. **Restart required** — resolved once at startup and registered as a process-global handle (#5963). See [Host-distress circuit breaker](#host-distress-circuit-breaker-4235) below |
| `autonomous.hostBreaker.loadPerCoreTrip` | `LOOM_HOST_BREAKER_LOAD_PER_CORE` | `2.5` | Load-per-core at/over which a tick counts toward tripping. `<= 0`/invalid → default |
| `autonomous.hostBreaker.sustainTicks` | `LOOM_HOST_BREAKER_SUSTAIN_TICKS` | `3` | Consecutive over-threshold work-finder ticks required to trip (a single spike never trips). Zero/invalid → default |
| `autonomous.hostBreaker.cooldownSecs` | `LOOM_HOST_BREAKER_COOLDOWN_SECS` | `300` | Cool-down window held after distress subsides before dispatch resumes. Zero/invalid → default |
| `autonomous.rateLimitBreaker.enabled` | `LOOM_RATE_LIMIT_BREAKER` | `true` | GitHub rate-limit circuit breaker on/off (#4429). A safety backstop — **defaults on**. Env truthy enables, any other value disables; wins over config. **Restart required** — resolved once at startup, before any loop is spawned (#5963). See [GitHub rate-limit circuit breaker](#github-rate-limit-circuit-breaker-4429) below |
| `autonomous.rateLimitBreaker.fallbackCooldownSecs` | `LOOM_RATE_LIMIT_BREAKER_FALLBACK_COOLDOWN_SECS` | `900` | Cooldown length when the `gh api rate_limit` reset probe fails. Zero/invalid → default; every computed cooldown is clamped to `[60, 3600]`s |
| `autonomous.perTokenConcurrency` | `LOOM_PER_TOKEN_CONCURRENCY` | *(none)* | **RETIRED — removed entirely (#5743, previously retired-from-the-cap-only by #5270).** Used to feed a `healthy × per-token` figure into the dynamic cap (#3947); since #5270 it fed only a disclaimed, informational `status`/`calibrate` line with no admission effect. Unlike the CPU knobs below, there is no ongoing deprecation warning for it — the key and env var are simply unread; a config file that still sets it parses fine (unknown keys are ignored, not an error) |
| `autonomous.cpuUtilizationTarget` | `LOOM_CPU_UTILIZATION_TARGET` | *(none)* | **DEPRECATED — accepted but ignored (#4512).** Fed the deleted CPU-headroom admission term (#3978, config surface #4032). Any value at any type still parses (never a config error); the daemon logs one warning per process naming it and `loom-daemon calibrate` prints it to stderr. Delete it to silence the warning; tune `autonomous.workFinder.maxConcurrent` instead |
| `autonomous.estCoresPerSweep` | `LOOM_EST_CORES_PER_SWEEP` | *(none)* | **DEPRECATED — accepted but ignored (#4512).** Same story as `cpuUtilizationTarget` above: the CPU term it sized is gone; heavy build/test stages are bounded by the [machine-wide build slot](#machine-wide-build-slot-4512) instead |
| `autonomous.mainHealthGate.enabled` | `LOOM_MAIN_HEALTH_GATE` | `false` | Gate loop on/off |
| `autonomous.mainHealthGate.ciWorkflow` | `LOOM_GATE_CI_WORKFLOW` | *(unset)* | Forge workflow that must itself conclude `success` for forge-CI corroboration to vouch for a commit (#3987). Empty/whitespace → unset. Absent → today's unanimity rule, unchanged. See [Optional named verification workflow](#optional-named-verification-workflow-loom_gate_ci_workflow-3987) |
| `autonomous.mainHealthGate.suppressDispatchDuringGate` | `LOOM_MAIN_HEALTH_GATE_SUPPRESS_DISPATCH` | `true` | Hold new dispatch off a root while its build-gate run is in flight (#4084), per-root so a sibling with no gate in flight keeps dispatching. Env truthy (`1`/`true`/`yes`/`on`) enables, any other value disables; wins over config. Set `false` to recover the pre-#4084 `is_halted`-only behavior. **Restart required** — resolved once at startup from the primary workspace config (#5963). See [build-gate.md → gate-in-flight dispatch suppressor](build-gate.md) |
| **`forge.githubApp.mintTimeoutSeconds`** (not `autonomous.*` — it lives beside the `appId` / `privateKeyPath` that `github-app-token.sh` itself reads) | `LOOM_GITHUB_APP_MINT_TIMEOUT_SECS` | `90` | Bound on one `github-app-token.sh get-token` subprocess (#5630). Raised from the pre-#5630 fixed `20` because on a saturated host (`observed_idle=0%`) fork/exec + the JWT sign + two GitHub round-trips routinely exceeded 20s, failing a refresh tick that succeeds in ~30ms by hand. Zero/invalid → default. The mint is additionally retried **once** on a transport-level failure (timeout / spawn error), never on a parsed `{"status":"error"}` answer |
| *(env only — n/a)* | `LOOM_FORGE_CREDENTIAL_STALE_GRACE_SECS` | `1800` | How long after the **first** failure of a consecutive credential-refresh-failure streak the main-health gate treats its forge answers as untrustworthy and holds each repo's previous verdict (#5630). Env-only: the credentials are daemon-global, so a per-repo config key would be ambiguous. Zero/invalid → default. See [Stale-credential gate hold](#stale-credential-gate-hold-5630) below |
| `autonomous.roleRunner.enabled` | `LOOM_ROLE_RUNNER` | `false` | Periodic standalone support-role runner on/off (#4015). **Resolved per registered root** (#4377) — see the callout below the table. **Live** — every `roleRunner.*` key (`enabled`, `roles`, `onIdle`, `model`, …) is re-read from that root's config on every role-runner tick, not cached at daemon startup; no restart needed for a config-only change (#5963) |
| `autonomous.roleRunner.roles` | *(config only)* | the 7 **interval-default** roles (`architect` excluded, #5656) | Subset of `champion`/`curator`/`judge`/`doctor`/`auditor`/`guide`/`hermit`/`architect` to dispatch on the interval cadence; explicit empty array runs none. **The absent-key default is the interval-default subset, not the whole table**: `architect` is idle-addressable-only (see `onIdle` below) and is never swept in by the "unset ⇒ all defaults" fallback — naming it here explicitly is the deliberate opt-in to a timer-driven architect (1h cadence). **Allowlist, not an addition** — must be updated by hand when a new interval-default role ships, or it silently never dispatches (#5339); a non-empty pinned list missing an interval-default entry warns, once per resolved-config change, in one workspace-named aggregated line (#6163) (omitting `architect` never warns — that is correct, not stale; neither does omitting a role named in `onIdle`, which dispatches on the idle edge instead). Also resolved from each root's own config |
| `autonomous.roleRunner.intervalSecs` | `LOOM_ROLE_RUNNER_INTERVAL_SECS` | per-role built-in — curator/judge/doctor 300s, champion/auditor/hermit 600s, guide 900s (5–15 min); `architect` 3600s, idle-addressable-only | Uniform override applied to every enabled role's cadence — **when either tier is set, every role logs the same interval and the per-role built-ins are entirely inert.** The boot log names which tier won: `role_runner: <role> interval=<n>s source=built-in|config:…|env:…` (#6204). Zero/invalid env → next tier |
| `autonomous.roleRunner.maxConcurrent` | `LOOM_ROLE_RUNNER_MAX_CONCURRENT` | the 7 interval-default roles | **The ceiling on concurrently-running role agents (#6102)** — the role-runner counterpart of `workFinder.maxConcurrent`, which bounds sweep dispatch **only**. Counted **process-wide across every managed workspace** (the host is shared; a per-root ceiling would bound nothing on a 25-workspace box) but resolved from each root's own config, like `architectMaxProposals`. A refused tick logs at `WARN` and retries next tick — distinct from the `debug!`-level per-`(root, role)` overlap skip (#4364). Zero/non-integer at either tier drops to the next (a `0` ceiling is `enabled: false` spelled confusingly). **Live** — re-read every tick. See [The other half of the agent budget](#the-other-half-of-the-agent-budget-role-runner-agents-6102) |
| `autonomous.roleRunner.model` | *(config only)* | `sonnet` | Model every role child is pinned to via `--model` (#4501). Resolved through the same `resolve_dispatch_model` chain as sweep dispatch: this key > `autonomous.model` > shipped default; blanks treated as unset. A role child never inherits the account's interactive CLI default |
| `autonomous.roleRunner.onIdle` | *(config only)* | `[]` (none) | Subset of all **8** shipped roles — the 7 above **plus `architect`**, which is reachable here and nowhere else by default (#5656) — to fire on the work-finder **idle edge** (#4364) — the non-idle → idle transition (0 in-flight sweeps AND nothing dispatched this tick), in addition to the interval cadence. Absent → none (opposite default from `roles`); unknown names ignored with a warning. Debounced to min 60s per (root, role) and skipped while that role's interval/idle run is in progress. **Requires the work finder enabled** to observe idleness (a startup warning fires if set with the work finder off). **Also gated by that same root's own `enabled`** (#4377) — see below |
| `autonomous.roleRunner.architectMaxProposals` | `LOOM_ARCHITECT_MAX_PROPOSALS` | `5` | **Per-invocation** cap on how many proposal issues one `architect` dispatch may file (#5656) — the actuator-saturation limit of the idle-edge control loop. Passed to the session as `/loom:architect --max-proposals <n>`, which `architect.md` enforces as a hard ceiling. Per-repo on purpose (the workable cap grows with a repo's maturity — ~5 while work is narrow, 7+ once it fans out), so it is read from each root's own config. Zero/negative/non-integer at either tier drops to the next one (a cap of `0` would spend a whole session forbidden from producing anything). Ignored for every other role |
| `autonomous.roleRunner.collisionDetection` | `LOOM_ROLE_RUNNER_DETECT_COLLISIONS` | inherits `autonomous.collisionDetection.enabled`, else `false` | Cross-host role-tick collision baseline (#4623). Detection only — a pre-tick probe of that role's own label queue, logged/counted, never acted on. Absent → falls through to #4085's shared toggle; see [Cross-host role-tick collision detection](#cross-host-role-tick-collision-detection-4623) |
| `autonomous.roleRunner.collisionWindowSecs` | `LOOM_ROLE_RUNNER_COLLISION_WINDOW_SECS` | that role's tick interval | Lookback window for the #4623 probe, clamped to `[60, 3600]`. Zero/invalid dropped to the next tier |
| *(host-local tiers only — see below)* | `LOOM_ROLE_RUNNER_SHARD_INDEX` | *(unset)* | **This host's** 0-based role-runner shard index (#6374). Must **differ** per host, so it belongs in the service unit next to `LOOM_ROLE_RUNNER`, never in the tracked `.loom/config.json` — a committed `autonomous.roleRunner.shardIndex` gives every host the same index and leaves every other slice with zero owners fleet-wide, so the daemon **refuses** it (logs `error!`, falls back to unsharded). Requires `shardCount`; out-of-range/malformed → unsharded. See [Role-runner host sharding](#role-runner-host-sharding-6374) |
| `autonomous.roleRunner.shardCount` | `LOOM_ROLE_RUNNER_SHARD_COUNT` | *(unset)* | Fleet-wide number of role-runner shards (#6374) — must be **identical** on every host, which is why the tracked config is a fine home for it. `0`/`1`/malformed → unsharded (every host rotates every workspace, the pre-#6374 behavior). Requires `shardIndex` |
| `autonomous.roleRunner.shardKey` | *(config only)* | `owner/repo` from `origin`, else the root's basename | Explicit cross-host-stable key hashed to pick a workspace's owning shard (#6374). Must be identical fleet-wide. Set it when the derived key would diverge between hosts — the basename fallback does exactly that if two hosts cloned the same repo into differently-named directories. `status` reports the resolved key and its tier so two hosts can be diffed |
| `autonomous.idleExit.enabled` | `LOOM_AUTONOMOUS_IDLE_EXIT_ENABLED` | `false` | End the daemon cleanly after the idle window so a host guard can take over. Independent of Work Finder; never invokes a power command |
| `autonomous.idleExit.idleMinutes` | `LOOM_AUTONOMOUS_IDLE_EXIT_MINUTES` | `60` | Continuous idle/starvation window. Zero/invalid → default |
| `autonomous.idleExit.onTokenStarvation` | `LOOM_AUTONOMOUS_IDLE_EXIT_ON_TOKEN_STARVATION` | `true` | Also exit after zero healthy accounts for the full window with no sweep in flight, even if roles keep cycling |
| `autonomous.watchMonitor.enabled` | `LOOM_WATCH_MONITOR` | `true` | Durable operator-watch monitor loop (#3971). Default-on; no dispatch side effect, zero forge calls until a watch is registered |
| `autonomous.watchMonitor.intervalSecs` | `LOOM_WATCH_MONITOR_INTERVAL_SECS` | `120` | Watch poll cadence. Zero/invalid → default |
| `autonomous.watchMonitor.expirySecs` | `LOOM_WATCH_MONITOR_EXPIRY_SECS` | `86400` | Give-up window for an unresolved watch; `0` disables expiry |
| `autonomous.dispatchStaggerMs` | `LOOM_SWEEP_DISPATCH_STAGGER_MS` | `2000` | Min gap between consecutive child spawns (#3887). `0` disables |
| `autonomous.watchdog.enabled` | `LOOM_SWEEP_WATCHDOG` | `true` | Startup watchdog on/off (#3887). Also the master switch for the tick — mid-build-death (#3895) + review-stall (#3910) run in the same task |
| `autonomous.watchdog.timeoutSecs` | `LOOM_SWEEP_WATCHDOG_TIMEOUT_SECS` | `300` | No-progress window before auto-restart (raised from 120s in #4088 — the old default sat inside the observed 110–150s dispatch→worktree window and cancelled healthy sweeps) |
| `autonomous.watchdog.intervalSecs` | `LOOM_SWEEP_WATCHDOG_INTERVAL_SECS` | `30` | Watchdog probe cadence (shared by all three backstops) |
| `autonomous.watchdog.reviewStall` | `LOOM_SWEEP_REVIEW_STALL` | `true` | Review-phase stall watchdog on/off (#3910) |
| `autonomous.watchdog.reviewStallTimeoutSecs` | `LOOM_SWEEP_REVIEW_STALL_TIMEOUT_SECS` | `2700` | Log-silence window before a hung Judge/Doctor sweep is re-dispatched |
| `autonomous.collisionDetection.enabled` | `LOOM_DETECT_COLLISIONS` | `false` | Cross-host dispatch-collision detection and enforcement (#4085, upgraded from detection-only by #5789). Off by default — adds one extra `gh issue view --json labels` round-trip per dispatch. When enabled, a confirmed pre-flip collision backs off the dispatch instead of only logging/counting it |
| `safehouse.enabled` | `LOOM_SAFEHOUSE_ENABLED` | `false` | Enables safehouse fleet-comms (#3997) **and** cross-host soft-claim coordination (#4028). Off by default — a byte-for-byte no-op (no socket, no coordination task) when unset |
| `safehouse.peerClaimTtlSecs` | `LOOM_PEER_CLAIM_TTL_SECS` | `120` | Peer-claim TTL, in seconds (#4028) — how long a peer's soft claim suppresses local dispatch (measured against local receipt, not the advertiser's clock). Default = 2× the 60s work-finder tick. Since #4431 live claims are re-advertised every reaper tick, so the TTL only bounds how long a **crashed** host's claim lingers |
| *(env-only)* | `LOOM_PEER_COORDINATION_DEGRADE_GRACE_SECS` | `600` (10m) | How long this host may advertise peer claims with **no** receive before peer coordination is judged DEGRADED (#6157), in whole seconds. 20× the 30s reaper re-advertisement cadence, so a handful of missed room round-trips never trips it while a genuinely one-way transport is caught in single-digit minutes. Zero/unparseable → default. A DEGRADED verdict surfaces as `loom-daemon health`'s `peer_coordination` section — diagnostic only since Epic #6165 Phase 4 (#6317): it no longer freezes stale-claim reclamation, which now gates solely on the lease record (#6286) |
| *(env-only)* | `LOOM_PEER_COORDINATION_RECOVERY_THRESHOLD` | `3` | How many **consecutive** genuine peer receives must land while coordination is DEGRADED before it is judged recovered (#6157). Self-advertisements never count, so a single stray ad cannot clear a verdict whose whole point was sustained receive absence. Zero/unparseable → default |
| `safehouse.rooms.signal` | `LOOM_SAFEHOUSE_ROOM_SIGNAL` | *(falls back to `safehouse.room`)* | Attention-class routing (#4225): the **signal** room id (`loom-fleet`) — operator conversation, every `handoff`, terminal `ack`/`completion`. Absent **and** no `byRepo` ⇒ single-room mode, byte-identical to pre-#4225 |
| `safehouse.rooms.byRepo` | `LOOM_SAFEHOUSE_ROOMS_BY_REPO` (`repo=room,…`) | `{}` | Attention-class routing (#4225): per-repo **firehose** room ids keyed by workspace-root basename — `task`/`chat` narration. A repo absent from the map is created lazily as `fleet-<repo>`; a refused creation degrades that repo to the signal room with one `warn!`. The env form replaces the whole map |
| `safehouse.claimReconcileIntervalSecs` | `LOOM_CLAIM_RECONCILE_INTERVAL_SECS` | `1800` when `safehouse.enabled`, else `600` | Periodic `loom:building`/PR-claim reconciliation cadence (#4431). With safehouse peer-claims carrying the fast in-flight signal (re-advertised each reaper tick), label reconciliation demotes to a slow healing sweep. Env wins on any host; floored at 60s |
| *(host identity)* | `LOOM_HOST_ID` | `$HOSTNAME` → `hostname` → `unknown-host` | This host's identity string, used in collision log records (#4085) **and** peer-claim self-recognition (#4028); set it where the daemon runs without `$HOSTNAME` exported |
| `autonomous.autoUpdate.enabled` | `LOOM_AUTO_UPDATE` | `false` | Autonomous self-update loop on/off (#4055). **Opt-in** (it rebuilds + restarts the daemon process). Exactly one loop per daemon, not a per-workspace fan-out. See [Autonomous self-update loop](#autonomous-self-update-loop-4055) below |
| `autonomous.autoUpdate.intervalSecs` | `LOOM_AUTO_UPDATE_INTERVAL_SECS` | `900` | Cadence between staleness checks. Zero/invalid → default |
| `autonomous.autoUpdate.settleSecs` | `LOOM_AUTO_UPDATE_SETTLE_SECS` | `600` | Settle window: wait this long after first observing a stale commit — resetting on every further commit — before rolling, so a burst of merges collapses into one roll. Zero/invalid → default |
| `autonomous.autoUpdate.deferDeadlineSecs` | `LOOM_AUTO_UPDATE_DEFER_DEADLINE_SECS` | `21600` (6h) | Bound on the build-stampede gate (#4929): after this much **continuous** deferral for in-flight sweeps, the rebuild runs anyway at reduced CPU priority (`nice 19`) instead of deferring forever. Any check that sees zero in-flight sweeps — or a new source commit, or a completed rebuild — re-arms the clock, so short busy bursts never reach it. Zero/invalid → default; there is deliberately no "defer forever" value (set a very large one instead) |

### Idle exit for remote hosts (#4467)

```json
{
  "autonomous": {
    "idleExit": {
      "enabled": false,
      "idleMinutes": 60,
      "onTokenStarvation": true
    }
  }
}
```

Ordinary idleness requires zero in-flight sweeps, no active role run, and no
dispatch/completion event for the full window. Token starvation has a separate
clock: role sessions do not reset it, but live sweeps and healthy-account
recovery do. On either trigger the daemon writes
`<loom-dir>/idle-exit.json`, publishes `daemon.idle_exit`, removes its socket,
and exits 0. It never calls `shutdown`, `sudo`, or a provider API.

The fleet add-worker systemd unit uses `Restart=on-success` (#4640, matching
`loom-daemon-start.sh`'s canonical unit), so exit 0 relaunches — same as macOS
launchd's `KeepAlive:SuccessfulExit`. Taken **in isolation**, idle exit only
meaningfully *stops the process* under on-failure-style supervision (e.g. a
non-systemd/nohup Linux host); under `Restart=on-success` (fleet workers,
launchd's `KeepAlive:SuccessfulExit`) the supervisor immediately relaunches
the daemon it just exited, so this stage alone never leaves the host
observably idle for long. A loud warning is logged when it is enabled under
launchd; the same caveat applies to any systemd-supervised daemon (fleet
workers included). **This no longer means the combination is pointless on a
fleet worker (#5565)**: the freshly-relaunched daemon re-runs the identical
tracker, so stage 2's cron guard — which asks the daemon's *current*
eligibility on every poll rather than checking whether the process is
merely alive — still correctly observes "idle" once the window elapses
again, relaunch or not. See step 9's `idle-shutdown` entry above for the
guard-side half of this.

**This is stage 1 only — it never powers off the host.** `autonomous.idleExit`
above just makes `loom-daemon` itself exit; under `Restart=on-success`
supervision (the fleet-worker default) the supervisor immediately relaunches
it, so the *host* stays running and reachable throughout. The actual
host-power-off mechanism is stage 2: the separate `fleet add-worker
--idle-shutdown-minutes` cron guard (`render_idle_shutdown()`) documented in
the `fleet add-worker` step 9 above — that is the one an operator needs a wake
path for (provider console/CLI restart, tailnet-identity survival,
re-registration), documented in full at that step, along with the #4697
`fleet status` "expected power-off" classification.

### Daemon log path override (`LOOM_DAEMON_LOG`, #4010)

`setup_logging()` writes every daemon log line to `$HOME/.loom/daemon.log` by
default — this is the destination for `env_logger`-routed output (see the
launchd redirect note below), NOT the on-disk `LOOM_SOCKET_PATH` used by IPC.
Before #4010, this path was hardcoded with no override at all, so **any**
`loom-daemon` process — including one spawned by an integration test that
already isolates its IPC socket via `LOOM_SOCKET_PATH` — still wrote into the
operator's production log, polluting real operator history (worst-case: enough
test noise to rotate genuine history out of the 10MB × 10-file window).

Two env vars now cover both the log path and the socket path:

| Env var | Purpose | Default |
|---------|---------|---------|
| `LOOM_DAEMON_LOG` | Full override of the daemon log file path | `<loom dir>/daemon.log` |
| `LOOM_SOCKET_PATH` | Full override of the daemon's IPC socket path; also implicitly derives the log path (below) when `LOOM_DAEMON_LOG` is unset | `<loom dir>/loom-daemon.sock` |

Precedence is **env > default only** (no `autonomous.*` config tier) — `resolve_log_path()`
checks `LOOM_DAEMON_LOG` first, then falls back to `resolve_loom_dir().join("daemon.log")`,
where `resolve_loom_dir()` is the parent directory of `LOOM_SOCKET_PATH` when
that env var is set, else `$HOME/.loom`. Practically: setting `LOOM_SOCKET_PATH`
to a tempdir (as both `loom-daemon/tests/common/mod.rs` and
`loom-daemon/tests/integration_singleton_guard.rs` already do to isolate IPC)
isolates the log file for free, with zero test-file edits. A config tier was
considered and deliberately dropped: `setup_logging()` runs at daemon startup
*before* workspace/config resolution happens, so wiring config in would require
restructuring startup order (and `env_logger` cannot be re-targeted after
`.init()`) for marginal benefit over the env-only surface — file a follow-up if
a config tier is wanted later.

**The launchd `StandardOutPath`/`StandardErrorPath` redirect is a decoy, not a
second log.** `defaults/scripts/cli/loom-daemon-start.sh` points both launchd
redirects at a single file, `$REPO_ROOT/.loom/logs/daemon-start.log`
(`$START_LOG`), which only ever captures output emitted *before*
`setup_logging()` installs the `env_logger` pipe target (plus a crash's raw
stderr, if one occurs before logging is set up). In normal operation this file
stays **0 bytes** — all real daemon logging goes through `env_logger` straight
to `daemon.log` (or wherever `LOOM_DAEMON_LOG`/`LOOM_SOCKET_PATH` points it).
This has repeatedly misled operators into concluding a running daemon was
silent or dead when they tailed `daemon-start.log` instead of `daemon.log`.

**Autonomous dispatch model (`autonomous.model`, #3944).** A daemon-dispatched
child is a headless `claude -p "/loom:sweep N"` process. Without an explicit
`--model`, it inherits whatever model the operator last configured in their
**interactive** CLI — which on the v0.15.0 canary was a premium tier that meters
premium usage credits and hard-failed every spawn with "out of usage credits". To
stop an autonomous fleet from ever silently inheriting a premium interactive
default, the daemon now pins an explicit model on **every** auto-dispatch
(work-finder sweeps, epic-supervisor role/child dispatches) and on
`dispatch_sweep` when its `model` param is absent. The resolved model is chosen by
this precedence, highest first:

1. **`dispatch_sweep` `model` param** — an explicit per-dispatch request always
   wins (unchanged from #3477).
2. **`autonomous.model`** in `.loom/config.json` — the per-repo default for all
   autonomous dispatch.
3. **Shipped default `sonnet`** — a deliberately **non-premium** tier (fast +
   cost-appropriate for the bulk of build work). Never a premium tier.

Empty/whitespace values are treated as unset at every tier. The resolved model
and the tier that supplied it are named in the daemon dispatch log line
(`… model=<m> (source=param|config|default)`), and the model is forwarded to the
child via the existing `--model` plumbing (#3705). Set `autonomous.model` to
`opus` (or any valid model id) to raise the autonomous default per-repo.

**Startup-race mitigation (#3887).** Rapid back-to-back dispatch (the work
finder draining a backlog in one tick) could wedge some `claude` children at
startup in a 0-HTTPS MCP-init race: the sweep log showed only the spawn header,
no worktree was created, and the issue never left `loom:building`. Two layers
now guard against it: the **dispatch stagger** spaces consecutive child spawns
out of the simultaneous-startup window (prevention), and the **startup
watchdog** probes each running sweep for progress (worktree created / checkpoint
written / log output past the spawn header) and auto-cancels + re-dispatches —
**exactly once, bounded, never a loop** — any sweep hung with no progress past
`timeoutSecs`. Both the auto-cancel and the retry log loudly and reuse the
frozen `sweep.issue.{N}.exited` / `sweep.global.completed` / `sweep.global.dispatch`
topics (no new event topics). The watchdog defaults **on**; disable it with
`LOOM_SWEEP_WATCHDOG=0` or `autonomous.watchdog.enabled = false`.

**Progress is latched per sweep (#4088).** The progress probe reads only the
*current* filesystem state (worktree / checkpoint / log), and **all three signals
are torn down at successful completion** — `merge-pr.sh` removes the worktree,
`/loom:sweep` deletes the checkpoint, and the log never grows past the spawn
header. So a *finished* sweep is structurally indistinguishable from one that
*never started*, and because the decision function is memoryless (`elapsed` is
total runtime and only increases), a completed-then-cleaned-up sweep would be
re-dispatched against its own — now closed — issue. To fix this the watchdog
**latches progress per `SweepId`**: once a sweep is ever observed making progress
it is left alone for the rest of its life (later crashes are delegated to the
mid-build-death / review-stall backstops below). The latch is keyed by `SweepId`,
not issue number, so a *re-dispatched* sweep that then genuinely hangs at startup
is still rescued. Independently, **`dispatch` skips any issue that is closed on
the forge** (best-effort, fail-open on a `gh` error) — this covers all three
watchdogs, since each re-dispatches through the same method, so no self-heal path
can ever re-claim a closed/merged issue.

**Mid-build-death live-use veto (#4449).** The mid-build-death watchdog (#3895)
infers "died mid-build" from `(terminal state ∧ no PR ∧ dirty worktree)` and then
resets the worktree (`git reset --hard` + `git clean -fd`) before re-dispatching.
That signature is necessary but **not sufficient**: on 2026-07-29 the daemon's
tracked sweep for issue #4366 had indeed died, but an *untracked* recovery session
was concurrently editing the same worktree — and the watchdog destroyed its
tested-but-uncommitted fix mid `git commit`. `git status` cannot tell dead-sweep
debris from a live session's work in progress, so the watchdog now also requires
that **nothing live still holds the worktree**. Four signals veto the reset (any
one is enough):

| Signal | What it means |
|--------|---------------|
| `.loom-in-use` marker in the worktree | A session (including a manual, non-daemon one) claims it. This is the one an operator can plant by hand to fence off a worktree. |
| `.loom/locks/issue-<N>/owner.json` whose `owner_pid` is **alive** | A live claim holder. A dead owner's lock is ignored, so a stale lock never wedges recovery. |
| `index.lock` in the worktree's gitdir | A git index write (`git add`/`git commit`) is in flight — precisely the #4449 window. |
| A live process whose cwd is inside the worktree | An operator shell, a manual role session, or an orphaned grandchild still writing files. |

A veto logs one loud `midbuild-watchdog: … REFUSING to git reset --hard …` line
naming the holder, leaves the worktree untouched, and **does not consume** the
single recovery retry — so a genuinely-dead sweep is still recovered on a later
tick once the holder goes away. If the holder is stale, clear it (remove
`.loom-in-use` / the claim-lock / the `index.lock`, or exit the shell) and the
next tick recovers normally. Independently, every `clean_worktree` call now logs
the porcelain status + diffstat of what it is about to destroy, so a wipe can
never again be silent in the daemon log. The same defense-in-depth guard exists on
the script side: `worktree.sh remove <N>` refuses a worktree with uncommitted
changes and requires an explicit `--force` to discard them.

**Review-phase stall watchdog (#3910).** The startup watchdog rescues a sweep
that shows *no* progress, and the mid-build-death watchdog (#3895) rescues one
that made progress then *died*. Neither covers a sweep that is **still alive**
but wedged in a hung role subagent — the observed failure was a `/loom:sweep`'s
internal Judge or Doctor `Task` running **49–66 minutes (multi-hour in the worst
cases) emitting zero output until the very end**, silently blocking the sweep's
back half with no self-heal. The third backstop, running in the same watchdog
tick, closes that gap: for each still-running daemon-dispatched sweep that has
already made startup progress, it measures **log silence** (how long the
per-sweep log file's mtime has gone un-advanced — a live sweep flushes tool
output continuously, a hung one does not) and, past `reviewStallTimeoutSecs`
(default 45 min), auto-cancels the wedged child and re-dispatches the issue
**exactly once, bounded, never a loop**. The re-dispatch resumes from the sweep
checkpoint, so the hung review phase is re-run — not the whole build. A second
stall resolves to give-up and surfaces on the frozen `sweep.issue.{N}.crashed`
topic (no new event topics). Gated to sweeps past startup so it never
double-acts with the startup watchdog on the same tick. Defaults **on**; disable
with `LOOM_SWEEP_REVIEW_STALL=0` or `autonomous.watchdog.reviewStall = false`.

> **Root cause & scope (#3910).** The stall is a *harness-side* artifact — a
> role subagent (`loom-judge`/`loom-doctor`) dispatched via the Claude Code
> `Task` tool that hangs on an opaque long-running / wedged tool call, producing
> no output until it eventually returns (or the sweep is killed). The
> **subagent-path** orchestrator (in-session `/loom:sweep`) cannot bound this
> from outside: it blocks awaiting each subagent's `TaskOutput` and the harness
> exposes no per-`Task` timeout or kill (see the "async-only dispatch" note in
> `sweep.md`), so the only in-session mitigation is prompt-level time-budget
> discipline in the role prompts. This watchdog is the **daemon-path** backstop
> — it works precisely because a daemon-dispatched sweep is an isolated OS
> process whose log file is observable and whose PID is cancelable, which the
> in-session `Task` is not.

The **gate's behavior** (which command runs against `main`, its timeout) still
comes from the separate top-level `buildGate` block (#3749); `autonomous.mainHealthGate`
is purely the on/off surface, so Phase C's already-tested `buildGate` semantics
are untouched. `LOOM_MAIN_HEALTH_GATE` remains the master override; the config
key just lets a repo turn the gate on without exporting an env var.

**Insta-crash quarantine (#3939).** The startup watchdog (#3887) and mid-build-death
watchdog (#3895) both rescue a sweep that made *some* observable progress before
dying. Neither covers the **insta-crash**: a child that dies within seconds of
spawn — e.g. a missing token pool or a selector import failure (#3938) — is
reaped, its `loom:building` claim restored to `loom:issue`, and the issue simply
re-qualifies on the very next work-finder tick. Left unchecked this occupies a
global concurrency slot forever and starves healthy candidates in other repos.
The reaper now counts **consecutive** insta-crashes per issue — a terminal
transition that wrote no phase checkpoint (never reached real work) and landed
within `instaCrashSecs` of dispatch. A death *with* a checkpoint, or a clean/slow
exit, resets the tally, so a genuine one-off failure never accretes toward
quarantine. After `threshold` consecutive insta-crashes the issue is
**quarantined**: the work finder skips it in-memory (no forge round-trip needed
for the load-bearing behavior) and, best-effort, flips the forge labels
(`loom:issue` → `loom:blocked`) with an explanatory comment so the pause is also
visible to a human. Quarantine is visible per-repo in `loom-daemon status`
(`quarantined (insta-crash, #3939): #123, #456`) and auto-releases after `ttlSecs`
— a transient breakage (e.g. a re-provisioned token pool) recovers without
operator action. To release a quarantine **immediately** (rather than waiting for
the TTL), run `loom-daemon quarantine clear <issue>`: it clears the daemon's
in-memory quarantine + insta-crash tally over IPC AND restores `loom:issue` on
the forge, so the issue re-qualifies on the next tick. **Note:** the in-memory
quarantine is the load-bearing state — manually flipping `loom:blocked` →
`loom:issue` on the forge alone does **not** release it (the work finder skips
the issue until the CLI clear or the TTL fires). In `tick_multi`, a quarantined
candidate is dropped **before** the global slot-fill pass, so a workspace whose
only candidates are quarantined never reserves a shared dispatch slot — healthy
sibling work in other repos gets it instead. Defaults **on**; disable with
`LOOM_WORK_FINDER_QUARANTINE=0` or `autonomous.workFinder.quarantine.enabled = false`.

**Per-issue dispatch backoff (#4485, step 2.8).** The quarantine above is a *three-strikes*
brake, and it has two deliberate carve-outs: a death attributed to account
exhaustion (#4122) or to a claude-wrapper pre-flight failure (#4386) leaves the
issue's insta-crash tally **untouched** on purpose — the issue is not at fault.
Nothing else limited how *often* one issue could be re-dispatched, so an issue
whose every dispatch died in a carved-out shape flapped
`loom:issue` ⇄ `loom:building` at the full reap → restore → re-poll cadence. The
observed incident: ~90 label events on one issue in ~7 minutes (claim, child dead
~4s later, claim restored, re-dispatched on the next tick), with the quarantine
not engaging for over 20 cycles. Two further amplifiers: the quarantine tally is
**per-process, in-memory** (two daemon instances sharing a workspace each need
their own three strikes), and nothing surfaced the flap in any log — it was found
by hand-reading the forge timeline.

The dispatch backoff caps the retry *rate* instead of diagnosing the *cause*. A
terminal outcome that made no progress **and** either died fast (inside
`instaCrashSecs`) or exited cleanly with zero lifecycle progress — including both
quarantine carve-outs — arms an exponential per-issue window (`baseSecs`, then ×2
per consecutive failure, capped at `maxSecs`); `dispatch()` refuses while the
window is live, **before** the claim lock and the label flip (step 2.8, immediately
after the #4444 park-label guard at step 2.7 — a deliberate park outranks a
transient backoff, so a parked *and* backed-off issue is reported as
`labeled-skip`), so a refusal costs no
forge write and cannot itself flap anything. A *slow* checkpoint-less death is
deliberately excluded — that is the mid-build-death (#3895) / review-stall (#3910)
watchdogs' remit. Any outcome that makes real progress clears
the window immediately, and a streak whose last failure is older than `maxSecs`
restarts from zero. The work finder also drops backed-off candidates before the
capacity gate (reported as `backoff-skip` on the per-tick summary line), so they
never reserve a shared dispatch slot. Deliberately narrow: in-memory only (a
daemon restart clears it — it can never permanently strand an issue), never
touches a forge label, bounded by `maxSecs` (well under the quarantine TTL, so
quarantine remains the longer, operator-visible brake), cleared by
`loom-daemon quarantine clear <issue>` alongside the quarantine, and released
before every **bounded one-shot recovery** dispatch — the reaper-driven resume
(#4256) and all three watchdogs (#3887/#3895/#3910) — so a refusal can never burn
a single-attempt recovery those latches already rate-limit.
Alongside it, the registry counts its **own** `loom:issue`/`loom:building` writes
per issue and logs a loud `LABEL FLAPPING` warning at 6 writes in 5 minutes
(a healthy dispatch writes exactly 2), so the pattern shows up in `daemon.log`
instead of only in the forge timeline. Disable with `LOOM_DISPATCH_BACKOFF=0` or
`autonomous.workFinder.dispatchBackoff.enabled = false`.

**No-op re-dispatch cooldown (#6670).** Both brakes above bound a *failing*
issue's retry rate; neither covers a sweep that runs to a clean, successful
conclusion and finds nothing to do — a phase checkpoint was written, the
`loom:building` claim was released cleanly back to `loom:issue`, and zero
forge/issue mutation happened. A standing/tracking issue with no pending
content change is the canonical shape: the observed incident was ~20
claim/release cycles on one issue in ~4 hours, including two dispatches an
hour apart that reached the *identical* "no actionable delta, releasing the
claim" conclusion with zero content change in between — each one burning a
full dispatch slot and agent session purely to re-confirm "still nothing to
do." Neither the quarantine nor the dispatch backoff trips on this shape: the
sweep made real lifecycle progress (it reached a genuine conclusion, not a
crash), so it looks identical to a normal healthy completion from the
reaper's point of view.

This mechanism is deliberately **call-through only** — unlike quarantine and
dispatch backoff, nothing in `reap_once`'s own terminal-outcome
classification infers it automatically. A sweep (or the `/loom:sweep`
orchestrator driving it) that reaches "no actionable delta this pass" reports
that back explicitly: `loom-daemon noop-cooldown record <N> --reason "..."`
(a positional issue argument, not an `--issue` flag — or the underlying
`RecordNoopRelease` IPC request), mirroring how `build-gate.sh` calls
`loom-daemon dispatch-backoff record` on a step timeout (#6192). Each call
arms (or refreshes) a flat `cooldownSecs` window — unlike the backoff's
exponential doubling, repeated no-op reports never grow the window, they just
re-arm the same one. The work finder drops a cooling-down candidate before
the capacity gate exactly like quarantine and backoff (reported as
`noop-cooldown-skip` on the per-tick summary line), and the three mechanisms
are fully independent: an issue can be quarantined, backed off, and cooling
down all at once, and clearing one never touches the others. `loom-daemon
quarantine clear <issue>` releases a no-op cooldown too, the same "let this
run now" operator lever it already applies to the dispatch backoff. Any
dispatch that goes on to make real checkpoint progress on the issue clears
its cooldown automatically. Defaults **on**; disable with
`LOOM_WORK_FINDER_NOOP_COOLDOWN=0` or
`autonomous.workFinder.noopCooldown.enabled = false`.

**First in-repo caller (#6740).** `defaults/scripts/record-noop-release.sh`
is the shell helper that actually makes this call — resolving the daemon
binary defensively (same `lib/locate-daemon-bin.sh` resolution
`build-gate.sh` uses) and treating a missing/unreachable/older `loom-daemon`
as a silent no-op, so the caller never fails on its account.
`/loom:sweep`'s Builder phase invokes it (see `sweep.md` → "Genuine no-op
conclusion vs. builder failure") right before releasing a `loom:building`
claim back to `loom:issue` when the builder's worktree carries an untracked
`.no-changes-needed` marker (builder.md → "Signaling No Changes Needed") and
no PR was opened — the exact "checkpoint written, claim released cleanly,
zero forge/issue mutation" shape this section describes. Hermetic call-shape
test: `defaults/scripts/tests/test-record-noop-release.sh`.

**Multi-workspace routing (#6957).** `record-noop-release.sh` never used to
pass `--workspace-root` on the caller's behalf, which is harmless on a
single-workspace daemon but silently misroutes on a multi-workspace one (the
epic supervisor's multi-repo fan-out, #3928): the IPC `RecordNoopRelease`
handler resolves its target registry via `resolve_registry`, which falls
back to the daemon's single cwd-seeded "default" registry whenever
`workspace_root` is absent — very unlikely to be the calling repo's own
per-repo registry (`WorkspacePool::get_or_provision`, the same call the epic
supervisor's dispatch guard uses to read cooldown state). A no-op recorded
against the wrong registry is invisible to the repo that needs it, producing
an unbounded re-dispatch loop on a candidate whose conclusion never changes
— observed live on a `loom:epic-phase` tracking issue blocked on a
`loom:operator-only` sub-issue (~25 identical no-op cycles over 5 hours).
The script now defaults `--workspace-root` to its own `$_repo_root`
(resolved via `git rev-parse --show-toplevel`) whenever the caller doesn't
supply one explicitly, so the cooldown always arms the correct registry
regardless of how many repos the daemon manages.

**Verified-open-PR memo (#6788).** The three brakes above bound how often an
issue is *re-dispatched*. A fourth, narrower problem sits one layer down, in the
#4123 open-PR guard's own probe: an issue whose closing PR is parked awaiting a
human (`loom:pr` + `loom:operator`, a Champion merge-risk hold) stays a
work-finder candidate for days, and the guard re-derived the same unchanged
"yes, PR #M is open" answer on **every tick** — measured on this repo's daemon
log, 4456 / 5380 / 5489 closes-graph queries against three such issues over five
days. That spend is a direct contributor to the GraphQL exhaustion that then
makes both the GraphQL probe and its #5911 REST fallback fail, dropping the
guard through its documented fail-open arm and re-dispatching an issue whose
work is already done. Across the four observed occurrences (#5936/#5914,
#6261/#6296, #6389/#6422, #6472/#6484), **91% of the fall-through dispatches
landed within 120s of a logged forge rate-limit event, against a ~12% baseline
for the dispatches the guard held** — the fail-open arm, not some other gap in
the dispatch path, is the dominant cause.

The memo attacks both ends of that loop with one piece of in-memory state,
written only from *verified* probe answers:

- A verified `Open(pr)` is reused for 15 minutes with **zero** `gh` calls, so a
  long-lived open PR is re-derived a few times an hour rather than once a
  minute. A verified "no open linked PR" invalidates it immediately, so a
  closed/merged PR never lingers.
- When both transports fail on every attempt, the guard re-verifies that one
  known PR over a single non-paginated `GET repos/{owner}/{repo}/pulls/{M}` —
  a call that needs neither the closes-graph nor a timeline walk, so it survives
  the conditions that killed both transports. Only a live `"open"` answer holds
  the guard.

The fail-open contract is unchanged: with no memo (a first probe, or any probe
after a daemon restart), or if the recheck itself cannot answer, or if the PR is
no longer open, the probe still concedes and dispatch still proceeds — a genuine
forge outage can never wedge the daemon. Disable with `LOOM_OPEN_PR_MEMO=0`.

**Note on #6740.** `noop_cooldown` above is *dispatcher-armed*: it only takes
effect once a completed sweep pass self-reports "no actionable delta this
time" via `loom-daemon noop-cooldown record`. `/loom:sweep`'s Builder phase
makes that call via `defaults/scripts/record-noop-release.sh` (#6740, see
"No-op re-dispatch cooldown (#6670)" above for the full mechanism and call
site). The two knobs below are independent of that wiring: they take effect
immediately, with no caller involvement required.

**Repo-local skip-label list (#6685).** `PARK_LABELS` / `SKIP_LABELS` (the
work-finder's own candidate-query filter, not the **Park-label dispatch
guard** documented below under
[Cross-host dispatch-collision detection and enforcement](#cross-host-dispatch-collision-detection-and-enforcement-4085-phase-0-of-4028-enforcement-added-by-5789))
are a hardcoded `loom:blocked` / `loom:operator-only` pair — there was
previously no way for a repo whose own label taxonomy uses a different name
(e.g. `blocked-upstream`, observed on `rjwalters/vibesql#6399`) to get the
same durable-park treatment without renaming the label to a `loom:*` name.
`autonomous.workFinder.extraSkipLabels` (env `LOOM_WORK_FINDER_EXTRA_SKIP_LABELS`,
comma-separated) supplies additional label names checked alongside
`SKIP_LABELS` via `WorkItem::is_skipped_with_extra`, resolved with the same
env > config > default precedence every other `workFinder.*` knob uses. See
the config table above for the full contract, including the `loom:building`
exclusion guard.

**Self-declared re-check interval (#6685).** Distinct from `noop_cooldown`
above in one important way: that mechanism is armed only *after* a sweep
pass observes "no delta," so a tracking issue known **up front** to need
infrequent polling still gets dispatched at full cadence until enough no-op
passes accumulate. A tracker issue can instead declare its own minimum
re-check interval directly in its body, in the spirit of the Curator's
`<!-- loom:complexity=<tier> -->` marker:

```
<!-- loom:recheck-interval=6h -->
```

The value is a bare duration — an integer optionally followed by `s`
(seconds, the default), `m` (minutes), `h` (hours), or `d` (days). The
work-finder skips a candidate carrying this marker whenever its own forge
`updatedAt` timestamp (already returned by the ETag-cached issue listing at
zero extra cost) is still within the declared interval — every dispatch and
every sweep-appended comment advances `updatedAt`, so it doubles as a
"last checked" clock without the work finder maintaining a separate one.
Reported as `recheck-interval-skip` on the per-tick summary line. Checked
independently of and without reading `noop_cooldown` state — an issue can be
in neither, either, or both cooldowns simultaneously, and clearing/setting
one never touches the other. No config knob: this is a per-issue, not
per-workspace, declaration, and lives entirely in the issue body.

### Host-distress circuit breaker (#4235)

The insta-crash quarantine above protects the shared **queue** from one broken
issue; the host circuit breaker protects the **host itself** from a sustained
overload. It was added after the 2026-07-27→28 meltdown (#4231): a 6-way sweep
fan-out drove load to 118 on 28 cores, the window server was watchdog-killed five
times, and the daemon itself crashed — and then a *second* wave re-spiked the host
two hours later because nothing remembered the first incident. The point-in-time
admission checks (the CPU headroom term #3978 — since removed in #4512 — and the
per-tick load-admission ramp cap #4234) each make a *fresh* decision every tick,
so the instant load dipped they re-admitted a full wave.

Since #4512 removed the CPU estimate from admission, this **measured** breaker is
also what makes a hand-tuned per-machine `maxConcurrent` safe to raise: a mis-set
knob trips the breaker — on observed load-per-core, not on a constant — instead
of melting the host, while the [machine-wide build slot](#machine-wide-build-slot-4512)
keeps the heavy stages serialized in the first place.

The breaker is the **stateful** layer those checks lack. It samples
**load-per-core** (`loadavg_1m / logical_cpus`) once per work-finder tick and runs
a three-phase state machine:

- **Closed** (normal) → **Open** once load-per-core has been at/over
  `loadPerCoreTrip` for `sustainTicks` *consecutive* ticks. A single spike resets
  the counter, so a transient burst never trips.
- **Open** (tripped) — new dispatch is **suppressed** in every repo while running
  sweeps **drain untouched** (the breaker never kills a running sweep). Stays Open
  while the host is still hot.
- **CoolDown** — once load drops back below the threshold, dispatch stays
  suppressed for a further `cooldownSecs` so a transient dip can't re-admit a full
  wave (exactly the #4231 01:41 re-spike). A re-spike over the threshold during
  cool-down sends it straight back to Open; otherwise the cool-down elapses and it
  returns to Closed.

The breaker's suppression composes with the existing dispatch suppressors at the
same choke point — it is OR'd onto the main-health-gate halt and the scheduled
drain, and works alongside (never bypassing) the `maxAdmissionsPerTick` ramp cap.

**Default-ON rationale.** Unlike the work-finder loop itself (opt-in,
default-off), the breaker **defaults on** — the same call the insta-crash
quarantine made. It only ever acts under genuinely severe *sustained* load, it
never aborts running work, and it is only ever *sampled* from inside the
work-finder loop, so a daemon that never enables the work-finder sees zero
behavior change. A backstop that defaulted off would not have prevented #4231's
second wave, which is the whole point.

**Explicit `dispatch_sweep` is hard-blocked by default (with a `force`
override).** This differs deliberately from the sibling headroom advisory (#4234),
which only *advises* an explicit dispatch that would exceed the point-in-time
dynamic cap and never blocks it. A *tripped* breaker represents **sustained,
already-observed** distress — a materially stronger signal than a single-tick
headroom reading — so `dispatch_sweep` (CLI `loom-daemon dispatch <N>` / the
`mcp__loom__dispatch_sweep` tool) is **refused** while the breaker is Open or
CoolDown. An operator who knows the host is distressed and wants to dispatch anyway
passes `--force` (CLI) / `force: true` (IPC / `mcp__loom__dispatch_sweep`).

**Observability.** The breaker is surfaced three ways: a log line on every phase
change; a state-change-deduped `daemon.host_breaker.state` event-bus event
(payload: `transition`, `state`, `reason`, `tripped_at`, `releases_at`); and
`loom-daemon status`, which prints `Host breaker: OK/OPEN/COOLING DOWN` with the
reason, the tripped-at time, and the cool-down release time (also in
`--json → host_breaker`).

**Clearing it manually.** The breaker auto-clears when the cool-down elapses on a
recovered host — no operator action is needed in the normal case. To clear it
sooner, either resolve the load (it releases on the next below-threshold tick plus
the cool-down) or restart the daemon (the state is in-memory, like the
quarantine). To disable it entirely, set `LOOM_HOST_BREAKER=0` or
`autonomous.hostBreaker.enabled = false`.

**Follow-ups deliberately deferred** (kept out of the first version to stay
minimal, per #4235): macOS-only trip signals (`.ips` crash reports, `syspolicyd`
CPU-ratio amplification, daemon self-restart detection) and durable trip/release
telemetry (coordinate with #4137 rather than building a parallel store).

### GitHub rate-limit circuit breaker (#4429)

The host breaker above protects the **host**; this breaker protects the
**shared forge API budget**. Every fleet host authenticates as the same
identity, so the REST/GraphQL rate limits are one fixed pool — and when it
exhausted on 2026-07-29, every polling loop on every host kept firing its full
per-tick call pattern for ~25 minutes (all failing), while role sessions were
spawned straight into the same wall.

The machine is simpler than the host breaker's — **Closed ⇄ Cooldown**, no
sustain counter, because a rate-limit rejection is unambiguous:

- Any gh polling failure whose stderr carries a rate-limit signature
  (GraphQL primary, REST 403, secondary-limit phrasings) **trips** the
  breaker. On trip, one `gh api rate_limit` probe — an endpoint that does
  *not* count against the quota — learns the real reset epoch; the cooldown
  runs to the latest exhausted resource's reset, clamped to `[60s, 3600s]`,
  falling back to `fallbackCooldownSecs` when the probe fails.
- While cooling, the work-finder, claim/quarantine reconciliation, epic
  supervisor, and role-runner ticks **skip entirely** — zero gh calls, zero
  doomed role spawns. Running sweeps are never touched.
- The breaker **releases itself** on the first tick past the reset. Edges are
  logged once each way and published as `daemon.rate_limit_breaker.state`
  events; `loom-daemon status` shows the phase, the tripping loop, the resume
  deadline, and the last-probed budget.

To disable it entirely, set `LOOM_RATE_LIMIT_BREAKER=0` or
`autonomous.rateLimitBreaker.enabled = false` (the pre-#4429
hammer-through-exhaustion behavior).

**Steady-state consumption** is attacked separately (#4428): the hot polls
(work-finder `loom:issue`, claim reconciliation `loom:building`, epic
supervisor `loom:epic`/`loom:epic-phase`) go through an ETag-cached REST
listing (`forge_listing`) instead of GraphQL `gh issue list` — a poll where
nothing changed answers `304 Not Modified`, which costs **zero** rate limit.
The cache is in-memory, per (workspace, query), for the daemon's lifetime; a
restart re-fetches each listing once. PR-side claim listings stay on
`gh pr list` (they need `headRefName`, which REST issue rows do not carry).

### Cross-host dispatch-collision detection and enforcement (#4085, Phase 0 of #4028; enforcement added by #5789)

When two `loom-daemon` hosts share one repo backlog, both can dispatch the same
issue: the `mkdir` claim lock (`.loom/locks/issue-<N>/`) is filesystem-local, so
a peer host's lock is invisible, and the `loom:issue → loom:building` flip
succeeds whether or not the label was still there — the losing host is never
told it lost. This makes the cross-host collision rate **unobservable**, which is
the prerequisite gap #4028's coordination layer has to justify closing.

Collision detection makes that rate **measurable**, and — since #5789 —
**enforced**: a confirmed collision now backs off the dispatch instead of only
being logged/counted. When enabled, just before the label flip the registry
reads the issue's **pre-flip** label state (`gh issue view <N> --json labels`)
and classifies it:

- `loom:issue` already gone **or** `loom:building` already present → **collision**
  (a peer host claimed it first). A diagnostic record is logged at `warn` — issue
  number, repo/workspace, this host's identity (`LOOM_HOST_ID` → `$HOSTNAME` →
  `hostname` → `unknown-host`), timestamp, and the observed pre-flip label set —
  a per-registry cumulative counter is incremented, and (#5789) the dispatch
  backs off instead of proceeding.
- `loom:issue` present and `loom:building` absent → **clean** (this host is first).
- gh timeout / non-zero exit / unparseable JSON → **unknown**. **Fail-closed:**
  an unverifiable read is never counted as a collision, so the baseline is never
  inflated.

**How to read the count.** The running total is surfaced on the work-finder's
per-tick summary line as the trailing `N cross-host-collision(s)` field, e.g.:

```
work_finder: tick — cap 3 (...); 5 seen, 2 dispatched, 1 labeled-skip,
0 in-flight-skip, 0 quarantine-skip, 0 pr-open-skip, 0 deferred, 0 error(s), 3 cross-host-collision(s)
```

Unlike the other (per-tick) counters, this one is a **monotonic cumulative
total** read from the dispatcher(s) at tick end (summed across workspaces in the
multi-repo tick), so successive lines show the baseline accumulate. It is `0`
whenever detection is disabled. The existing fields (`labeled-skip` /
`in-flight-skip` / `quarantine-skip` / `pr-open-skip` …) keep their names and
semantics unchanged.

**`pr-open-skip` (open-PR dispatch guard, #4123).** A per-tick counter (like the
other `*-skip` fields, not cumulative) for issues the finder declined to dispatch
because they **already have an open linked PR**. The guard lives in
`SweepRegistry::dispatch()` (step 2.6, right after the #4088 closed-issue guard),
so it covers *every* dispatch caller — the work finder, the epic supervisor, the
IPC/CLI `dispatch_sweep`, and all three watchdogs — with one forge check. Every
in-memory dedup signal (idempotency key, in-flight set, `loom:building` label)
dies with the parent sweep, so without this guard an issue whose approved PR is
still open would be re-dispatched the moment its sweep exits, redoing finished
work against the token pool. `dispatch()` refuses these with a typed
`OpenPrDispatchError`; the finder attributes that refusal to `pr-open-skip`
(never to `error(s)`), and a tick whose only outcome is a `pr-open-skip` **still
logs** its summary line (the counter is part of the log gate). The guard keys on
PR *openness* only — never on review labels — and is **fail-open**: any forge
error, timeout, or unparseable output lets dispatch proceed, so a `gh` outage can
never wedge the daemon. It is GitHub-only (uses the `closedByPullRequestsReferences`
closes-graph, filtered to `state == "OPEN"`); a Gitea workspace fails open and
keeps today's behavior.

**A large held-PR backlog can masquerade as work starvation here — a distinct
mechanism from #5715.** Because the guard keys on openness only, a PR that
Champion has put on a merge-risk hold (`loom:pr` + `loom:operator`) is still an
"open linked PR" as far as `pr-open-skip` is concerned, so its issue keeps
getting correctly declined every tick. When many PRs accumulate in that held
state, the ready queue can look starved from the outside — few or no fresh
dispatches, a string of `pr-open-skip` counts — even though the work behind
those issues is already done and merely stuck awaiting a human decision. This
is **not** the [Starvation escape hatch (#5715)](#starvation-escape-hatch-5715)
condition above: that mechanism detects host-load starvation (admission held,
zero sweeps in flight) and has a bounded escape hatch; this one has no escape
hatch, because bypassing it would re-dispatch and redo already-finished work.
The only fix for a held-PR-backlog read of "starvation" is clearing the
operator holds (merge or close the held PRs) — see `champion-pr-merge.md`'s
"Held-PR Census" for the operator-facing view of that backlog.

**Park-label dispatch guard (#4444, step 2.7).** The `loom:blocked` /
`loom:operator-only` **park** labels are the state machine's only "take this out
of automation" signal, and until #4444 they were enforced *only* in the
work-finder's candidate query (`SKIP_LABELS`) — one of six dispatch routes. Every
other route (all three watchdogs, the reaper's checkpoint-resume, the epic
supervisor, the IPC/CLI `dispatch_sweep`) funnels through
`SweepRegistry::dispatch()` without re-reading the forge labels, so a park applied
*after* the original dispatch was invisible to them and the daemon re-claimed
deliberately parked work (observed on #4366). Step 2.7 — immediately after the
2.6 open-PR guard, still before the claim lock and label flip — refuses any
dispatch whose issue currently carries a park label, with a typed
`ParkedIssueDispatchError` the work finder attributes to `labeled-skip` (never to
`error(s)`). Four properties are load-bearing:

- **`PARK_LABELS`, not `SKIP_LABELS`.** The guard keys on the
  `loom:blocked` / `loom:operator-only` subset only — `loom:needs-capability`
  (#5817) is not (yet) a member; extending the Rust daemon's own dispatch
  guard to the new label is out of scope for #5817, which covers
  `/loom:sweep`'s skip parity only (see `.loom/docs/label-state-machine.md`).
  `loom:building` is
  *legitimately* present on a watchdog re-dispatch or a checkpoint-resume of the
  daemon's **own** claim, so refusing it would break exactly the recovery paths
  this guard is meant to constrain. The two constants live side by side in
  `work_finder.rs` (`SKIP_LABELS` is composed as `BUILDING_LABEL` +
  `PARK_LABELS`), so they cannot drift.
- **The #4256 resume bypass does not exempt it.** `resume_bypass_pr` exempts step
  **2.6** for a sweep's own open PR and nothing else — a park applied after the
  crash still stops the resume. The refusal stays failure-visible: the reaper
  logs it and still publishes `sweep.issue.{N}.resume_dispatched` with
  `dispatched: false`.
- **REST, not GraphQL.** The probe is `gh api repos/{owner}/{repo}/issues/{N}
  --jq '.labels[].name'`, a *different* rate-limit bucket from the GraphQL calls
  steps 2.5/2.6 ride — so a park still holds while the GraphQL quota is
  exhausted, the condition under which the #4123 guard failed open during the
  2026-07-29 incident.
- **Fail-open and fixture-safe.** Any `gh` error, timeout, or unresolvable repo
  lets dispatch proceed (a forge outage must never wedge the daemon), and the
  probe is skipped entirely when label flips are disabled (`skip_label_flip`).

Operator note: because this covers the IPC/CLI route too, a manual
`dispatch_sweep` on a parked issue is refused by design — clear the park label
first (or use `loom-daemon quarantine clear <N>` when the park came from the
insta-crash quarantine) rather than expecting a manual dispatch to override it.

**Cost / default.** Enabling detection adds exactly one `gh issue view --json
labels` round-trip per dispatch (measured ~0.4s against GitHub), bounded by the
same `LOOM_REAP_GH_TIMEOUT_SECS` ceiling as the other best-effort `gh` calls.
Because that is a real per-dispatch API cost it is **off by default**; enable it
per the config/env row above (`LOOM_DETECT_COLLISIONS=1` or
`autonomous.collisionDetection.enabled = true`, precedence **env > config >
default**) on the hosts sharing a backlog. This is no longer a pure measurement
knob (#5789): enabling it also means a confirmed pre-flip collision backs off
that dispatch instead of only being logged/counted.

### Cross-host role-tick collision detection (#4623)

The #4085 probe above covers **sweep dispatch** only. The standalone role runner
(Champion / Curator / Judge / Auditor / Guide ticks) had the same blind spot with
none of the instrumentation: its `RoleRunGuard` overlap guard is an in-memory
`HashSet<(root, role)>` — one *process* — and `loom-daemon-start.sh`'s PID-file
guard is one *host*. Two daemons (two hosts, or two clones on one host each with
their own PID file) pointed at the same forge repo with
`autonomous.roleRunner.enabled=true` therefore ran the same role with zero mutual
awareness. That is the leading explanation for #4586's eight duplicate "Cannot
Auto-Merge" Champion comments on PR #4540 inside ~5 minutes, far above the
documented 10-minute Champion cadence; a repo that leaves the
`.github/workflows/loom-*.yml` cron schedules enabled *alongside* the daemon role
runner has the same shape.

**The signal: foreign activity on the role's own queue.** Each role acts on a
label-defined queue, and any pass over it bumps `updated_at` on the items it
touches. Immediately before a tick for `(root, role)` the daemon lists that queue
over the **ETag-cached REST listing** (#4428 — an unchanged queue is a `304` at
*zero* rate-limit cost, and it never touches the GraphQL budget), then compares
the newest `updated_at` against the wall-clock window of **this process's own
last completed tick**. Activity strictly after our own pass finished (plus a 90s
clock-skew margin) was not written by us.

| Role | Probed queue |
|------|--------------|
| `champion` | open PRs labeled `loom:pr` |
| `judge` | open PRs labeled `loom:review-requested` |
| `curator` | open issues labeled `loom:curating` |
| `guide` | open issues labeled `loom:triage` |
| `auditor` | open issues labeled `loom:auditor` |

Outcomes mirror #4085's three-way classification: **collision** (logged at `warn`
with role, repo, host identity, the newest item and its timestamp, the lookback
window, and a running process-lifetime count), **clean**, or **unknown** —
fail-closed, so no self-run baseline yet (the first tick after daemon start) and
any listing failure are never counted.

**Detection only.** Nothing suppresses, delays, or reorders a role invocation; a
detected collision is one log line. Enforcement is #4028 Phase 2's cross-host
CAS, deliberately out of scope.

**Read the count as a baseline, not proof.** It **under**-counts (a peer pass
whose writes land inside our own tick's window is invisible; a pass that changes
nothing writes nothing) and can **over**-count in a bounded way (a human, a
Builder, or a *sweep-internal* Judge/Doctor/Champion touching the same queue is
indistinguishable here). The `warn` line says so explicitly.

**Config.** The role runner honors #4085's shared toggle directly — the same
problem class should need one switch — but accepts a role-runner-specific
override, because the two probe shapes have different costs and cadences (one
`gh issue view` *per dispatch* vs. one cached REST listing *per role per tick*).
Precedence, highest first:

1. `LOOM_ROLE_RUNNER_DETECT_COLLISIONS` (env; `1`/`true`/`yes`/`on` enable, any
   other value — including an explicit `0` — disables even when the shared
   toggle is on)
2. `autonomous.roleRunner.collisionDetection` (config, bool)
3. `LOOM_DETECT_COLLISIONS` / `autonomous.collisionDetection.enabled` (#4085)
4. default **off**

The lookback window is `LOOM_ROLE_RUNNER_COLLISION_WINDOW_SECS` >
`autonomous.roleRunner.collisionWindowSecs` > that role's own tick interval,
clamped to `[60s, 3600s]`. Defaulting to the role's cadence is the natural
choice: a peer running the same role on the same cadence writes to the queue at
least once per interval. Like every other per-root role-runner knob, enablement
is resolved from **each registered root's own** `.loom/config.json` (#4377); the
daemon logs its own workspace's resolution once at startup.

**Reaper-driven resume (#4256): the guard's own escape valve.** A sweep that
dies **after** its Builder opened a PR would otherwise strand that PR at
`loom:review-requested` forever — the #4123 guard above correctly refuses
every *ordinary* re-dispatch of the issue (an open PR looks identical to
fresh work the instant the sweep exits), but nothing else ever re-dispatches
it, so the checkpoint-resume machinery (#3373, which would skip straight to
Judge) never gets a chance to run. `SweepRegistry::reap_once()` closes this
gap directly: when a crashed sweep's checkpoint reads `builder-done`,
`judge-rejected`, `judge-done`, or `doctor-done` (Builder-or-later — a
pre-Builder `curator-done` crash gets ordinary crash handling only) **and**
the issue still has an open linked PR, the reaper re-dispatches the *same*
issue through a private bypass (`dispatch_resume_after_crash`) that is
reachable **only** from the reaper — not from the work finder, the epic
supervisor, the IPC/CLI `dispatch_sweep`, or any watchdog — and only exempts
step 2.6 when the PR it names matches the one the guard itself would find.
Every other dispatch path still refuses via `OpenPrDispatchError`, so
#4123's anti-duplicate property is unchanged. The attempt is never silent:
a `sweep.issue.{N}.resume_dispatched` event (`{pr, checkpoint_phase?,
dispatched, repo?}`) is published — with `dispatched: false` on the rare
case the resume dispatch call itself fails — and narrated over Safehouse
(when enabled) as a `handoff`. Operator parks still win: `restore_label_to_ready`
preserves a `loom:blocked` label instead of flipping it back to `loom:issue`
(#4206), and the resume dispatch itself is refused by the **step 2.7 park-label
guard** (#4444) — which the 2.6 resume bypass deliberately does not exempt — so a
park applied after the crash surfaces as `dispatched: false` rather than an
overridden park.

**A clean exit is not a crash (#5614).** A surviving checkpoint proves the sweep
skill never reached its delete-on-success step; it does **not** prove the sweep
died. A run that ends with `exit_code == Some(0)` finished its turn
deliberately, and when it also left the checkpoint byte-identical to the one it
inherited, it reached a considered terminal decision and changed nothing —
canonically because the linked PR carries an engine-stop state such as
Champion's `loom:operator` merge-risk hold, where every sweep correctly reports
"held for a human" and exits 0. Resuming that shape re-runs an identical
decision over identical inputs, so it is guaranteed to repeat the same no-op
while costing an agent spawn, a rotated token, and *two* forge label writes per
cycle (the reaper's `restore_label_to_ready`, then the resume's re-claim). That
is the observed #5565 flap: seven dispatches in seven minutes, all exit 0, ~10
`loom:issue`/`loom:building` transitions. The reaper therefore refuses to resume
a clean exit that made no checkpoint progress, publishing the usual
`dispatched: false` event. Three properties keep the narrowing safe:

- **#4256's actual remit is untouched.** Insta-crashes, account exhaustion,
  signal deaths, and stall-then-kill never carry `Some(0)`; a no-handle reap
  reports `None`, which is not `Some(0)`, so reconstructed entries keep
  pre-#5614 behavior (fail-open toward resuming).
- **Real progress still resumes regardless of exit code**, via the same
  `checkpoint_written_by_run` test the quarantine tally uses — including the
  #4366 parked-mid-turn case, whose signature is a clean exit that *did* advance
  the lifecycle.
- **It consumes no resume attempt.** A human-gated pause is not a failed
  attempt, so clearing the hold leaves the full `MAX_RESUME_ATTEMPTS` runway
  intact. Nor is the work stranded: `restore_label_to_ready` has already
  returned the issue to `loom:issue`, where the #4123 open-PR guard refuses
  ordinary re-dispatch and the periodic Judge/Champion roles own the open PR —
  the same resting state attempt-cap exhaustion produces, reached without
  burning the attempts first.

### Role-runner host sharding (#6374)

Collision *detection* above tells you a peer host ran the same role tick you
did. Host sharding stops it happening: on a fleet, each workspace's role
rotation runs on **exactly one host per interval**.

**Why.** The role runner is a per-host loop with no cross-host coordination, so
on N dispatchers every workspace's rotation runs N times per interval — N
`claude` sessions over the same forge queue. On the 2AMLogic fleet (4
dispatchers × 27 workspaces × 900s) that drew the token pool down to 2/17
available with role ticks failing ~20/hour, and it is the shape of the #6332
docs-PR race and the #6352 narration duplication. The operator mitigation was
`LOOM_ROLE_RUNNER=0` on some hosts — correct in direction, but all-or-nothing:
it cannot spread 27 workspaces over 4 hosts. Sharding is the first-class
version of that knob, and `LOOM_ROLE_RUNNER=0` is its degenerate case.

**Mechanism.** Each host carries a shard index in `0..count`. A workspace's
rotation runs on the host whose index equals `fnv1a64(shardKey) % count`.
Because every host computes the same hash over the same key and the indices
partition the ring, exactly one host owns each workspace **by construction** —
no election, no lease, no window in which a workspace has zero or two owners.
FNV-1a is used rather than `DefaultHasher` deliberately: the invariant requires
two different processes, on different machines and possibly different Rust
versions, to agree on the value, and `DefaultHasher`'s algorithm and seeding are
both unspecified.

**The two knobs have deliberately opposite homes:**

| Knob | Where it belongs | Why |
|---|---|---|
| `shardIndex` | **host-local only** — `LOOM_ROLE_RUNNER_SHARD_INDEX` in the service unit (systemd `Environment=` / launchd `EnvironmentVariables`), next to where `LOOM_ROLE_RUNNER` is set today, or an untracked config tier | It must **differ** per host. Two hosts sharing an index own the same slice, and nobody owns the rest |
| `shardCount` | either — env or the tracked `.loom/config.json` | It must be **identical** fleet-wide, and a committed file is identical fleet-wide by construction |
| `shardKey` | tracked `.loom/config.json` | Same reason |

Declaring `shardIndex` in the **tracked** `.loom/config.json` is the one
misconfiguration that breaks the fleet in the worst direction — every host reads
the same file, resolves the same index, and every workspace not hashing to it
gets **zero** role ticks anywhere. The daemon detects that case specifically and
**refuses to shard**, logging at `error!` and falling back to unsharded. An
index supplied via `LOOM_ROLE_RUNNER_SHARD_INDEX` is per-host by construction
and legitimately overrides the (ignored) tracked value.

**Shard key precedence**, highest first: `autonomous.roleRunner.shardKey` >
`owner/repo` from the workspace's `origin` remote > the workspace root's
basename. The basename fallback is the one place the invariant can still break —
two hosts that cloned the same repo into differently-named directories hash
differently — so the resolved key **and its tier** are surfaced in `status`
(`key_source`: `config` / `git-remote` / `basename`) precisely so two hosts'
output can be diffed.

**Fail-safe direction.** Every malformed, incomplete, or contradictory
configuration resolves to *unsharded*, which owns **every** workspace — i.e. the
pre-#6374 behavior. Duplicating is wasteful but survivable and visible; the
opposite failure (a slice with no owner) silently stops role rotation and is
much harder to notice. `LOOM_ROLE_RUNNER=0` is checked **before** sharding, so
the blunt kill switch is never weakened or second-guessed by shard state. Both
dispatch surfaces are gated — the interval cadence and the `onIdle` edge —
because an idle edge fires on every host that observes it.

**Visibility.** `loom-daemon status` prints a `Role runner (sharding): …` header
whenever sharding is *configured* (including every misconfiguration — silence is
exactly how the `LOOM_ROLE_RUNNER=0` mitigation became invisible), and a per-root
line naming the owning shard, the key, and the key's tier. Unconfigured
single-host installs print neither. `--json` carries the same under
`role_runner_shard` (report-level) and `per_repo[].role_runner_shard`.

**Static, not roster-driven (deferred).** The assignment comes from
`(shardIndex, shardCount)`, not from a live host roster. Killing a host does
**not** automatically reassign its slice — those workspaces stop rotating until
an operator lowers `shardCount` or points a survivor at the vacated index.
Automatic reassignment needs a liveness protocol whose failure modes are exactly
the zero-or-two-owner races this static scheme rules out arithmetically, so it is
deliberately a follow-up (#6704) rather than part of the same change.

### Completion narration → public fleet feed (#4426)

When a sweep exits, the narration sink additionally asks the forge whether that
issue's PR actually **merged** (`gh pr list --head feature/issue-N --state
merged`, in the event's workspace root). If it did, the sink emits a second
envelope of type **`completion`** carrying a `completion-v1` `meta`
(`{schema, agent, repo, ref, result, started_at, completed_at, issue,
tokens?, title?, additions?, deletions?}`) alongside the human `ack`.
safehoused's egress mirrors well-formed `completion` envelopes out of allowlisted
rooms to its `sink_url` — that is what fills the public fleet feed. Notes:

- **`repo` is the forge `owner/repo` slug** (`gh repo view --json nameWithOwner`,
  cached per workspace), not the path-basename narration convention (#4201);
  `ref` is the PR URL.
- **Display fields (#4497)** back the feed's
  `<repo>#<issue>: <title> +A −D · <dur> · <tokens> tok` rows.
  `title`/`additions`/`deletions` come from the *same* `gh pr list` call that
  verifies the merge (zero extra round-trips) and degrade per-field; `tokens` is
  a best-effort input+output total from the in-process activity DB's per-issue
  rollup, whose attribution is knowingly imperfect (bare issue number, no repo
  column; only linked samples counted) but consistent enough for a cost trend.
  All four are optional — absent, the envelope is identical to the pre-#4497 one.
- **Exit 0 ≠ merged.** No merged PR ⇒ no completion, so `result: "success"` is
  never claimed for unmerged work. `result: "failure"` is not emitted in v1 —
  `completion-v1` requires a `ref`, and a sweep with no merged PR has no
  meaningful one (the wire support exists for a follow-up).
- **At most one per merge**, deduped on `(workspace, issue)` for the daemon's
  lifetime; downstream ingest is additionally idempotent on `event_id`.
- **No new config and no new event-bus topic** — this rides the existing
  `SweepExited` event and the `safehouse.*` block. Same degradation contract as
  all narration: a failed/absent/slow `gh`, invalid `meta`, or an unreachable
  safehoused drops the completion silently and never touches the sweep.

### Cross-host soft claim over safehouse (#4028, Phase 1 of #4028)

Where collision detection (above) only *measures* the race, the soft claim
*shrinks* it. When `safehouse.enabled` is true, each daemon advertises its
dispatch claims into the shared safehouse room and consumes peer advertisements,
so a peer daemon backs off before the non-atomic `loom:building` label flip would
let it race. This is Phase 1 of #4028 — see
[`.loom/docs/safehouse.md` → Peer-claim coordination](safehouse.md#peer-claim-coordination-cross-host-soft-claim-4028)
for the full design.

**Advisory-only for reclamation (Epic #6165).** The dispatch-time backoff
described here is unaffected by Epic #6165 — it is exactly #4028's original,
non-load-bearing role. What Epic #6165 changes is a *different* consumer of
this same channel: `claim_reconciliation`'s decision to reclaim an
already-`loom:building` issue, which for a period also froze while peer
coordination was judged DEGRADED (#6157, config rows below). That freeze was
superseded by the fleet-scoped lease record ([`lease-record.md`](lease-record.md),
Phase 2 #6286) and removed from the reclamation decision path entirely by
Phase 4 (#6317) — see [`safehouse.md` → Peer-claim
coordination](safehouse.md#peer-claim-coordination-cross-host-soft-claim-4028)
for the full note.

- **Advertise before the flip.** In `SweepRegistry::dispatch()`, right after the
  local claim lock and **before** `flip_label_to_building`, the daemon publishes a
  claim advertisement (issue, cross-host-stable repo slug, host identity, PID,
  timestamp) over the room. It rides a **`task`** envelope — the envelope `type`
  enum is closed and owned by the safehouse repo, so **no type is invented here**
  (loom only *uses* the members safehoused already defines) — with the bare issue
  number as `task_id` and the structured payload (`loom_claim`-marked JSON) in
  the `body`.
- **Dedicated inbound read task.** A coordination task on its own safehouse
  connection drains the socket continuously (`select!` over read + outbound), so
  an **idle** daemon that emits no narration still sees peer claims promptly — the
  narration sink only reads while it is emitting, so peer-claim consumption cannot
  piggyback on it. Inbound claims fold into a shared `PeerClaimView`
  (`loom-daemon/src/peer_claims.rs`, pure/socket-free).
- **Back off with a distinct counter.** The work-finder skips any issue with a
  live peer claim, counted as **`peer-claim-skip`** on the per-tick summary line —
  its **own** reason, never folded into `cross-host-collision(s)` (a post-hoc
  count) or the label/in-flight skips:

  ```
  work_finder: tick — cap 3 (...); 5 seen, 1 dispatched, 0 labeled-skip,
  0 in-flight-skip, 0 quarantine-skip, 0 pr-open-skip, 1 peer-claim-skip,
  0 deferred, 0 error(s), 0 cross-host-collision(s)
  ```

- **TTL + retraction.** Peer claims expire after **`safehouse.peerClaimTtlSecs`
  (env `LOOM_PEER_CLAIM_TTL_SECS`; default 120s = 2× the 60s work-finder tick)**,
  measured against **local receipt time** (never the advertiser's wall clock —
  clock skew is not comparable across hosts), so a crashed peer cannot
  permanently starve an issue. A peer also emits a `retract` ad from its reaper on
  a terminal sweep outcome, freeing the issue before the TTL.
- **Re-advertisement heartbeat (#4431).** The dispatch-time ad is no longer
  one-shot: the reaper re-advertises every live (`Running`/`Pending`) Issue
  sweep's claim each tick (default 30s — ~4 refreshes per TTL window), and a
  repeat ad **restarts** the receiver's TTL clock. A live sweep's claim
  therefore never expires from peers' views mid-run, while a crashed host's
  claims still free within one TTL of its last heartbeat. This is what lets
  the peer claim serve as the **primary fast in-flight signal** on
  safehouse-enabled hosts, with the `loom:building` label demoted to a slow
  healing audit trail:
- **Safehouse-conditional reconciliation cadence (#4431).** On a host with
  `safehouse.enabled`, `claim_reconciliation`'s periodic pass defaults to
  **1800s** (30 min, healing-only) instead of 600s. Precedence:
  `LOOM_CLAIM_RECONCILE_INTERVAL_SECS` env >
  `safehouse.claimReconcileIntervalSecs` config > 1800s (safehouse) / 600s
  (no safehouse), floored at 60s. Hosts without safehouse are byte-for-byte
  unchanged.
- **Self-claim recognition.** A daemon never backs off on its own advertisement:
  the claim body carries the host identity (`host_identity()`:
  `LOOM_HOST_ID` > `$HOSTNAME` > `hostname` > `unknown-host` — loom's single,
  derived, restart-stable host concept), and the view ignores ads from this host.
  safehoused's socket `from` is stamped from the *persona* (all daemons share
  `loom_daemon`) and cannot distinguish hosts, hence the body-carried identity.
- **Fail-open, no-op when off.** An unreachable/refusing/timing-out socket, a
  malformed envelope, or a full outbound channel is logged once and **dispatch
  proceeds** — the outbound ad is a bounded non-blocking `try_send` off the
  dispatch path. `safehouse.enabled` false/absent is a **byte-for-byte no-op**: no
  view, no channel, no coordination task, no socket. No new event-bus topic is
  added (the frozen taxonomy is untouched — claims travel entirely over the room).

> **Soft claim, NOT a mutex.** A room broadcast is eventually consistent, so this
> is a fast backoff, not a lock — two hosts advertising near-simultaneously still
> race. The **atomic** cross-host claim authority (a real CAS, e.g. a `git push`
> to a claim ref) is **Phase 2 of #4028**, filed separately.

### Prerequisite: a fresh token ranking (#3894, self-refreshed by the daemon since #3969)

**When you run autonomous mode against a multi-account token pool, keep
`.loom/tokens/.ranking` fresh.** The spawn-time selector (native `loom-daemon
tokens select`, cut over from `loom_tools.tokens.select` in #4228) is 3-tier —
ranking → allowlist → random — and the ranking file is only
considered fresh for **10 minutes**. When it is absent or stale, tier-1 declines
and selection falls to the lower tiers. The work finder dispatches in bursts, so
a stale ranking means the daemon can steadily hand out accounts a recent probe
already flagged `exhausted`/`blocked`, whose sweeps then wedge at startup (spawn
header logged, no worktree, ~0% CPU) — the exact failure the startup watchdog
(#3887) then has to self-heal, one hang at a time.

As of #3969 the running `loom-daemon` **keeps this fresh itself** — see
[Token-ranking self-refresh](#token-ranking-self-refresh-3969) below. A
standalone operator cron (the historical requirement) is now an **optional
fallback** for setups that don't run the daemon at all (a bare `/loom:sweep`
subagent-dispatch install with no `loom-daemon` process); see that section for
the cron example. Two things keep a burst of issues from wedging on a stale
ranking regardless of which refresher is running:

- **A refresher running on a `<10`-min cadence** — the daemon's built-in loop
  (default 10 minutes, comfortably inside the freshness window) or an operator
  cron. One-shot before a run: `loom-daemon tokens check --ranking`.

- **Stale-ranking fail-safe (selector-side, #3894).** Even without a fresh
  probe, a stale-but-present `.ranking` is no longer discarded. The selector
  treats its `exhausted`/`blocked` entries as an **advisory exclusion set** for
  the allowlist and random tiers, so it stops degrading to fully-random
  selection into known-exhausted accounts. If those exclusions would empty the
  pool (a stale "everything exhausted" ranking), selection retries ignoring them
  so a live pool never hard-fails on stale advice. This is a safety net, **not**
  a replacement for keeping the ranking fresh — a stale ranking still can't see
  an account that recovered.

### Token-ranking self-refresh (#3969)

The daemon runs its own periodic refresher for `.loom/tokens/.ranking` instead
of depending on an operator-managed cron for `probe-tokens.sh --ranking` — the
manual step documented above through #3894 is now automatic whenever
`loom-daemon` is running. It is the natural home for this loop: the daemon
already owns dispatch cadence and consumes the ranking (via `spawn-claude.sh`
selection) on every sweep it spawns.

**What it does.** On a configurable cadence (default 10 minutes) the loop
invokes **its own binary** (`std::env::current_exe()`) with `tokens check
--ranking --workspace <repo_root>` for each registered repo — as of #4080 this
is a direct daemon-to-daemon subcommand invocation rather than a shell out to
`probe-tokens.sh`, which sidesteps the "stale binary predates the `tokens`
subcommand" hazard entirely (the running daemon by construction supports its
own subcommands) and drops a process layer (daemon → bash → daemon). It probes
every bootstrapped account for its current rate-limit headers and atomically
rewrites `.ranking` in whichever pool [`tokens_pool::paths`] resolves for that
repo — the per-repo `.loom/tokens/`, or the shared machine-level pool (#3938)
when the per-repo pool is absent/empty.

**Default-on, unlike the work finder / main-health gate.** Those two loops are
opt-in because they have dispatch-affecting side effects (spawning sweeps,
halting dispatch). This loop only ever reads rate-limit headers and rewrites a
bookkeeping file nothing else consults synchronously, so it ships on by
default — an absent refresher would silently regress every install back to the
stale-ranking failure mode #3894/#3969 exist to fix. It still has a full opt-out
knob for a repo that wants it off (e.g. no tokens bootstrapped at all):

```json
{
  "autonomous": {
    "tokenRankingRefresh": { "enabled": true, "intervalSecs": 600 }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_TOKEN_RANKING_REFRESH` | `autonomous.tokenRankingRefresh.enabled` | env > config > default | `true` (on) |
| `LOOM_TOKEN_RANKING_REFRESH_INTERVAL_SECS` | `autonomous.tokenRankingRefresh.intervalSecs` | env > config > default | `600` (10 min) |

**Never fatal, never double-writes unsafely.** A probe failure (network hiccup,
`gh`/`python3` missing, every account exhausted, no tokens bootstrapped at all)
is logged and skipped — it never panics the loop or the daemon. Because
`loom-daemon tokens check --ranking` writes `.ranking` atomically (temp file +
rename), an operator's cron running the identical script concurrently is
harmless: the two refreshers can race to *schedule* a write but never to a
*torn* file, so keeping an existing cron alongside the daemon costs nothing but
a redundant API call.

**Multi-workspace.** Like the work finder / main-health gate, the loop re-reads
the workspace registry each tick and refreshes every registered repo's own
pool, gated by that repo's own config (an empty registry reduces to the single
daemon workspace). See `loom-daemon/src/token_ranking_refresh.rs` for the
implementation.

**This loop's scope is per-repo; `loom-daemon health`'s tokens section used to
be single-pool only (#5269).** This refresher keeps every registered repo's
OWN pool fresh independently — but through v0.18.0, `loom-daemon health`/
`status` reported staleness for only the daemon's single
`fallback_root`-anchored pool (its launch CWD, see [Full anchoring precedence,
and machine-level daemon startup](token-pool.md#full-anchoring-precedence-and-machine-level-daemon-startup-4292)
in token-pool.md), which on a multi-repo daemon is not necessarily any
particular *other* registered repo's own pool. `status`'s `per_repo` array now
carries each repo's own `token_pool_dir`/`ranking_present`/
`ranking_age_secs` (populated with the same unanchored `resolve_tokens_dir`
this loop uses), and `health --json`'s `tokens.detail.per_repo` surfaces it —
see [token-pool.md's `loom-daemon health` distinction](token-pool.md#loom-daemon-healths-daemon-cwd-vs-operator-repo-distinction-5269)
for the full incident writeup and the now-obsolete `$HOME`-refresh workaround.

### Merged-PR worktree reaper (#4876)

CLAUDE.md states the contract: *"Loom-managed worktrees (with the
`.loom-managed` sentinel) are auto-removed when their PR merges."* Through
v0.17.0 that had exactly **one** implementation — `merge-pr.sh`'s
`_remove_loom_worktree()`, a synchronous side effect of that script running —
so the removal only happened when all three of these held:

1. the merge went through `merge-pr.sh` (not the forge UI, `gh api`, or an
   auto-merge queue), **and**
2. it ran on the host holding the worktree, **and**
3. it ran in the checkout holding the worktree (`find_main_repo_root()` walks up
   from the invoking shell's cwd; there is no host-wide scan).

A multi-host fleet violates (2) constantly: worker-1 builds issue #N, studio
merges the PR, and worker-1's worktree has nobody to observe the merge. Nothing
daemon-side covered the gap. Observed on 2026-08-01: **44** stale worktrees on
`loom-worker-1` (54G under `.loom/`, disk at 81%) and 35 on studio — 16 on each
were merged-PR worktrees eligible for removal.

**What it does.** On a cadence (default 15 minutes) the daemon walks every
registered workspace, enumerates `issue-<N>` directories under that repo's
worktree root, and removes the ones whose issue is closed and whose PR merged
outside the grace period. The decision comes from *forge state*, not from
observing a merge event, so it does not matter which host merged or whether any
merge script ran.

**It can only ever remove a subset of `clean --safe`.** The gate chain is
`worktree_ops::clean::classify_worktree` — the same function the interactive
`loom-daemon clean` CLI uses, so there is no second copy to drift. The reaper
runs it with `safe: true, force: false`, which preserves a worktree on any of:
a live spawn-loop task or claim-lock, a `.loom-in-use` marker, a process whose
cwd is inside it, an editable pip install pointing into it, an open issue, an
open/absent PR, an unreadable forge probe, a merge still inside the grace
period, or any uncommitted change. It adds **one gate the CLI does not have**:
the `.loom-managed` sentinel is *required*. An unattended remover has nobody at
the keyboard to say no, so a user-provisioned worktree is never touched.

**A closed-without-merge PR's worktree eventually reclaims too (#6418).**
Unlike a merged PR, `main` never holds a closed-without-merge branch's
commits, so this case is gated by two conditions layered on top of the
grace-period check: its own 30-day grace period since the PR **closed**
(`CLOSED_NO_MERGE_GRACE_PERIOD_SECS`, deliberately much longer than the
10-minute merged-PR default — not currently configurable), and proof that
every commit on the branch is reachable from some remote ref (`git rev-list
--count <branch> --not --remotes` returning `0`) — the same check `clean
--safe`'s stale-branch pass (#5737) already uses. Either gate failing (no
resolvable close timestamp, or an unpushed/partially-pushed branch) keeps the
worktree indefinitely, same as before this issue.

**REST, not GraphQL.** The forge probes use `gh api repos/{owner}/{repo}/...`
rather than `gh issue view` / `gh pr list`. GraphQL quota exhaustion under
concurrent agents is a live failure mode, and an unattended cadence prober must
not compete with interactive agents for the scarcer pool. A failed probe reads
as `UNKNOWN`, which is always a *skip*.

**Low-disk warning.** Each pass also reads `worktree_root_free_gb` (the metric
the dashboard already renders but nothing acted on) and logs a `WARN` when free
space on the worktree-root volume is under the floor. That converts "the disk
filled and sweeps started failing with unrelated build errors" into an explicit
signal. An unmeasurable probe is skipped, never treated as zero (#4164).

**Default-on**, like the token-ranking refresh: it restores an
already-documented behavior whose absence is a slow-motion outage (a full disk
stops a host running sweeps at all).

```json
{
  "autonomous": {
    "worktreeReaper": {
      "enabled": true,
      "intervalSecs": 900,
      "gracePeriodSecs": 600,
      "diskWarnFreeGb": 20
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_WORKTREE_REAPER` | `autonomous.worktreeReaper.enabled` | env > config > default | `true` (on) |
| `LOOM_WORKTREE_REAPER_INTERVAL_SECS` | `autonomous.worktreeReaper.intervalSecs` | env > config > default | `900` (15 min) |
| — | `autonomous.worktreeReaper.gracePeriodSecs` | config > default | `600` (10 min) |
| `LOOM_WORKTREE_REAPER_DISK_WARN_GB` | `autonomous.worktreeReaper.diskWarnFreeGb` | env > config > default | `20` |

#### Pressure-triggered deep clean (#5919)

**The remaining leak.** The passes above reclaim *worktrees* — the whole
directory when its PR merged, or just `target/`/`node_modules/` from a kept-but-
idle one (#5187). Neither can touch the one directory that is neither a worktree
nor removable: the registered repo's **own primary checkout**. Only
`clean --deep` reclaims from `repo_root` itself, and the reaper never ran it. On
a long-lived host that leaked monotonically: 34 GB of `target/` regrowth in
about a day, a host down to 1.9 GiB free with its daemon alive but unqueryable
over IPC, and a `dynamic_cap` of 5 against a configured 12 — half the host's
concurrency lost to artifacts nobody was reclaiming.

**What it does.** At the end of each reaper tick, the daemon measures free space
on the volume holding **the repo itself** (not the worktree-root volume, which
may be a different disk) and, when it is below the floor, runs the equivalent of
`loom-daemon clean --deep --safe` against the primary checkout — removing
exactly `target/` and `node_modules/`, through the same code path the manual
command uses. It logs at `WARN` naming both what it reclaimed and why it fired.

**Pressure-triggered, not "deep every tick"** — flipping the 15-minute pass to
`deep` would delete a developer's warm build cache four times an hour on a host
with plenty of disk. A `minIntervalSecs` cooldown (default 6h) additionally
keeps a disk that is full for some *other* reason (a big dataset) from turning
into a delete/rebuild/delete loop.

**Safety.** `safe: true` is enforced at runtime, not by convention: the pass
refuses to run at all if handed non-`safe` or `force` options, so a scheduled
bare `--deep` is unreachable by construction. It also **holds the machine-wide
build slot** for the duration of the removal, so it can never delete `target/`
under a running build gate — if the slot cannot be taken it defers to the next
tick rather than proceeding unserialized (a consequence worth knowing:
`LOOM_BUILD_SLOTS=0` therefore also disables scheduled deep cleans, and the log
line says so). It never deletes a directory holding a running binary, and an
unmeasurable `df` never fires a deletion (unknown != zero, #4164).

**Live-binary protection (#6127).** "Never deletes the directory holding the
running binary" originally meant *this daemon's own* binary — a `current_exe()`
comparison, which cannot see a co-located service. It now also means **anyone
else's**: before removing `target/`/`node_modules/`, the shared engine both this
pass and the CLI `clean --deep` go through scans the process table (Linux
`/proc/<pid>/exe`, macOS/BSD `ps -o comm=`) and keeps the whole directory if a
live process is executing a binary inside it, logging at `WARN`:

```
deep_clean: /home/u/GitHub/safehouse kept target/ is backing 1 live process(es)
[pid 4127 → /home/u/GitHub/safehouse/target/release/safehoused] — refusing to
unlink a running program's binary. Stop the service (or, better, stop launching
it from a build-output path) and re-run.
```

That check is an ungated floor: no `--force` override, no config toggle, since
an escape hatch a scheduled job could set would restore the original bug (the
deletion is silent — a running process survives its binary being unlinked, and
the outage only fires at the next restart). It cannot see a service that is
**stopped** when the pass runs, so the operator-side rule still holds: install
what you run, and never point a launchd/systemd unit at a path under `target/`.
See [troubleshooting → Never launch a service from a build-output
path](troubleshooting.md#never-launch-a-service-from-a-build-output-path-6127).

```json
{
  "autonomous": {
    "worktreeReaper": {
      "deepClean": {
        "enabled": true,
        "freeGb": 20,
        "minIntervalSecs": 21600
      }
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_DEEP_CLEAN` | `autonomous.worktreeReaper.deepClean.enabled` | env > config > default | `true` (on) |
| `LOOM_DEEP_CLEAN_FREE_GB` | `autonomous.worktreeReaper.deepClean.freeGb` | env > config > `diskWarnFreeGb` | `20` |
| `LOOM_DEEP_CLEAN_MIN_INTERVAL_SECS` | `autonomous.worktreeReaper.deepClean.minIntervalSecs` | env > config > default | `21600` (6h) |

**Observability.** `loom-daemon status` prints an `artifact reclaim (deep, per
repo)` block beside the dynamic-cap disk term — when a pass last fired, what it
reclaimed, and (the common case) why the last evaluation declined, e.g.
`118G free >= 20G floor — no disk pressure`. The same records are on
`status --json` under `deep_clean[]`, so a watch loop can assert reclamation is
alive:

```bash
loom-daemon status --json | jq '.deep_clean[] | {root, last_fired_at, last_reason}'
```

The firing record (and therefore the cooldown) is **process state**: a daemon
restart re-arms it, which is harmless — the artifacts are already gone, so the
next pass reclaims nothing and re-arms. `last_fired_at: null` means "not since
this daemon started", not "never". See `loom-daemon/src/deep_clean.rs`.

**Not limited to the daemon's attached workspace.** The loop walks
`WorkspaceRegistry::effective_roots()` each tick, so a daemon started from
`~/GitHub/anvil` still reaps `~/GitHub/loom` as long as that repo is registered
(`loom-daemon workspace add <path>`). A repo that is *not* registered is still
not reaped — run `loom-daemon clean --safe --deep` there, or register it.

**Idempotent and recoverable.** A missed pass costs nothing: the next tick
re-evaluates from forge state, and a manual `loom-daemon clean --safe` remains a
superset recovery path that reports zero errors when the loop already cleaned
up. The first tick after daemon startup is deliberately skipped so in-flight
sweeps can re-establish their `.loom-in-use` markers first. See
`loom-daemon/src/worktree_reaper.rs`.

#### `pr-<N>` worktrees are reaped too (#5939)

Through v0.18.11 every automatic reclaim path was scoped to the `issue-<N>`
naming class. The directory scan itself always enumerated `.loom/worktrees/`,
but each entry was then resolved through `naming::issue_from_worktree`, which
recognizes only `issue-<N>` — so a `pr-<N>` worktree (created by
`.loom/scripts/pr-worktree.sh` for a PR whose branch does not fit the
`feature/issue-<N>` convention) was enumerated and immediately discarded.
Nothing on a timer could ever reclaim one; only a hand-run `loom-daemon clean
--aggressive` could, and that mode walks a vestigial-worktree decision tree
over *every* `git worktree` entry, so it is not something to schedule.

Measured on `loom-worker-1` (2026-08-10): 125 worktree entries, of which
**110 were `pr-*` holding 27 GB** — long-merged PRs against a repo ~600 PRs
further on. The scheduled cleaner was working correctly and reclaiming
nothing, because everything it could see had already been taken; disk fell
from 20 GB to 6.8 GB free over 2.5 hours and pinned `dynamic_cap` at 4 against
a configured 12.

The reaper now runs a second, parallel pass over `pr-<N>` directories:

- **Same safety gates.** `worktree_ops::clean::classify_pr_worktree` applies
  the identical in-use / active-process / editable-install / `.loom-managed`
  sentinel / grace-period / uncommitted-changes chain
  `classify_worktree` does. Both removal passes share one enumeration and one
  gate loop, so the `pr-<N>` class cannot drift from the `issue-<N>` class.
- **PR-number-keyed eligibility.** A `pr-<N>` worktree has no backing Loom
  issue, so there is no closed-issue gate and no issue-keyed claim-lock to
  consult. Its PR status is resolved directly from its own number via
  `gh api repos/{owner}/{repo}/pulls/<N>` (REST, like every other reaper
  probe); a 404 or any failure reads as `UNKNOWN`, which is always a skip.
- **Its branch is read, not constructed.** An `issue-<N>` worktree's branch is
  always `feature/issue-<N>`. A `pr-<N>` worktree's is whatever
  `gh pr checkout` produced, so it is read back from the worktree itself — and
  `main`/`master`/`develop`/`trunk` are never deletion candidates, the one
  guard the issue-keyed path does not need.
- **Artifact reclaim too.** A **kept** `pr-<N>` worktree (open PR, dirty tree,
  unresolvable PR status) gets its `target/` / `node_modules/` reclaimed
  in place, exactly as AC3 of #5177 already did for `issue-<N>`. Those are the
  long-lived worktrees by definition, so they are the ones whose multi-GB
  build directories sit on disk for days.
- **`--aggressive` is unchanged.** The genuinely risky classes — HEAD
  unreachable, unknown provenance, worktrees outside `.loom/` — remain
  `--aggressive`-only. The 29 "would lose work" skips in the measurement above
  are the correct default and stay that way.

**Worktree footprint in `loom-daemon status`.** The same issue added a
`Worktree footprint:` section under the dynamic-cap breakdown, reporting each
managed repo's worktree count split by naming class plus its total on-disk
size (and the matching `worktrees[]` array in `--json`). Before it, a host
carrying 39 GB of merged-PR worktrees rendered identically to one that was
genuinely out of space. The residual `other` bucket — entries matching neither
`issue-<N>` nor `pr-<N>` — is called out explicitly, so the *next* naming
class the reaper does not recognize is visible the day it appears rather than
after it has eaten a disk. It is collected client-side by the CLI (a
filesystem walk), never inside the IPC handler, for the same reason the
per-token usage table and `--pipeline` are.

### Orphaned-process reaper (#5110)

**The problem.** An agent's *background process tree* can outlive the agent. On
2026-08-03 `loom-worker-1` sat at **load 65 on 8 cores for 5h52m with no active
Loom work**: an agent-generated driver script inside `.loom/worktrees/issue-87`
had lost its parent, reparented to `systemd --user`, and kept issuing fresh
batches of simulations forever. Killing the running sims did nothing — the
driver launched eight more within 20 seconds. The host's *own* dispatched sweep
was starved to 0.6% CPU for 5h33m while holding a forge claim it could not
advance.

**Why the pgid teardown could not reach it.** A dead sweep's process group is
signalled with `kill(-pgid, …)` (#4982/#4980), but GNU `timeout` puts its child
in a **new process group** unless `--foreground`, so that tree spanned three
groups and two sessions — a group kill reaps one of them. And once the sweep
leader is gone its descendants have reparented, so there is no parent link left
to walk either. The only durable link between a runaway process and the work it
belongs to is **the worktree it is running in**.

**What it does.** On the worktree-reaper tick (before the directory pass), for
each `issue-<N>` worktree that is provably unowned, the daemon snapshots
`/proc`, attributes processes to the worktree by **cwd inside it** or **argv
referencing it**, expands each hit to its **whole descendant tree** through the
ppid map (which survives `setpgid`/`setsid` where the pgid does not), and
terminates it **freeze-first**: `SIGSTOP` parent-first so a looping driver
cannot issue another batch, a re-snapshot to catch anything forked in the
meantime, then `SIGTERM` + `SIGCONT`, and `SIGKILL` only for survivors after a
5s grace. Every pid killed is logged with its age and argv.

**Fail-safes.** A worktree's processes are touched only when **all** hold: the
`.loom-managed` sentinel is present (user-provisioned worktrees are never
touched), no `.loom-in-use` marker, **no live sweep owns the process** (see
"pid-scoped, not issue-scoped" below), **no live agent runtime working inside
the worktree** (a `claude`/`codex` process, or any argv naming a `/loom:` slash
command — the incident's defining property is that the agent was *gone*, and
this is what covers PR-set sweeps and manually driven agents that claim no
issue), the seed process is older than `minAgeSecs`, and the tree contains
neither this daemon nor its ancestors nor its own children. Every ambiguity
(unreadable `/proc` entry, unknown process age, non-Linux host) resolves to
*skip* — under-reaping is recoverable on the next tick, a false-positive kill is
not.

**Pid-scoped, not issue-scoped (#5135).** A live spawn-loop task / claim-lock,
or a `live_claim::probe` hit, used to protect the **whole worktree** — so an
orphan tree that predates and is unrelated to a concurrently live,
*re-dispatched* sweep for the same issue could never be reaped, which is exactly
the #5110 shape. The claim gate now reads the claim lock's own
`owner_pid`/`acquired_at` back out (`active_locked_issue_roots`) and, when that
owner pid is a confirmed-live non-zombie process, protects **only that sweep's
own process tree**: its root pid, every ppid-descendant of it, and any other
attributed pid that cannot be proven to have started *strictly before* the
claim was acquired. Everything provably older is still a reap candidate. The
same narrowing applies to the agent-runtime veto — an agent inside the resolved
live sweep's own tree no longer vetoes the whole worktree; any other agent still
does. When the root **cannot** be resolved (a stale lock, an unparseable owner,
a claim tracked only through the legacy spawn-loop-state union with no lock dir),
there is nothing to discriminate against and the whole worktree is protected
exactly as before. An orphan tree that overlaps the live sweep's tree at all is
refused whole rather than partially reaped, and the claim is re-resolved
immediately before each kill: a claim that appeared or *changed* since the plan
was built fails closed.

**Default-on**, with `dryRun` for operators who want to watch it first (it
detects and logs the trees it *would* reap and signals nothing).

```json
{
  "autonomous": {
    "processReaper": {
      "enabled": true,
      "minAgeSecs": 1800,
      "dryRun": false
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_ORPHAN_PROCESS_REAPER` | `autonomous.processReaper.enabled` | env > config > default | `true` (on) |
| `LOOM_ORPHAN_PROCESS_REAPER_MIN_AGE_SECS` | `autonomous.processReaper.minAgeSecs` | env > config > default | `1800` (30 min) |
| `LOOM_ORPHAN_PROCESS_REAPER_DRY_RUN` | `autonomous.processReaper.dryRun` | env > config > default | `false` |

**Operator note.** `minAgeSecs` is the knob that keeps an *interactive* command
you are running inside an issue worktree safe: a process is only ever a reap
seed once it is older than that floor **and** nothing claims its issue. If you
routinely run long jobs by hand in a finished issue's worktree, raise
`minAgeSecs`, drop a `.loom-in-use` file in the worktree, or turn the pass off.
Linux-only (it needs `/proc`); on other hosts the pass is a no-op. See
`loom-daemon/src/orphan_process_reaper.rs`.

### Install/host invariant self-check (#5035)

**The problem.** On 2026-08-03 a single manual cross-host check turned up **nine**
distinct install/host drift conditions — an empty `@modelcontextprotocol/sdk`
under `mcp-loom/node_modules` (which produced zero work for hours, #5016),
`.loom/runtimes/` absent in seven managed clones (#5002), a stale token
`.ranking` driving the wrong concurrency cap, and more. Every one is
mechanically checkable and most are mechanically repairable, yet Loom detected
some and acted on **none** — they were all found by a human asking. This loop is
the daemon verifying the invariants its own operation depends on.

**What it does.** On a cadence (default 30 minutes) the daemon walks every
registered workspace and, for each, checks a named set of install/host
invariants. Per the repo's repair-over-gate stance it **repairs what is
mechanically safe and idempotent, and files one deduped issue per
non-repairable / repair-failed condition** — but only when repair mode is
explicitly enabled (see below).

**The invariant set is single-sourced in code.** The authoritative list is
`Invariant::ALL` in `loom-daemon/src/install_self_check.rs` — this doc
deliberately does not re-enumerate the per-invariant check details, so the code
and the docs cannot drift (an acceptance criterion of #5035). This increment
covers the three conditions with confirmed live-outage / wrong-behavior
consequences on 2026-08-03:

- **`mcp-bundle-health`** (#1 / #5016) — `dist/index.js` present, and
  `node_modules` complete (no empty `@modelcontextprotocol/sdk`) when a lockfile
  exists. Repair: `npm ci && npm run build` (npm resolved explicitly — PATH
  hazards, #4875 — and **skipped under a live sweep**, which could pull the rug
  on an in-flight MCP session).
- **`runtimes-present`** (#4 / #5002) — every configured runtime has a populated
  `.loom/runtimes/<runtime>.json`. Repair: an idempotent copy-converge from
  `defaults/runtimes/` (byte-for-byte no-op when current, precedent:
  `update-gitignore` #4280).
- **`token-ranking-fresh`** (#5) — the token-pool `.ranking` is present and
  younger than the staleness threshold. Repair: re-probe via `tokens check
  --ranking` (the same mechanism the token-ranking-refresh loop uses).

The remaining six conditions (binary freshness, sidecar reachability, telemetry
identity, sweep liveness, pid file, stale repo-local `.mcp.json`) are a future
increment — adding one is a matter of extending `Invariant::ALL` plus one
check/repair arm each.

**Report-only by default, and default-off overall.** Unlike the default-on
reaper / token-ranking-refresh loops, this loop is **default-off** (FLAGS-OFF).
Even once enabled it runs **report-only** by default — it observes and logs
without touching anything, so the check can be trusted before it is allowed to
act. Repair (which also files issues for what it cannot repair) is a second,
separate opt-in. Filing exactly one issue per condition (deduped on a hidden
`<!-- loom:install-self-check:<id> -->` marker) is what avoids the
duplicate-comment stacking failure mode (#4736).

**It cannot wedge dispatch.** The checks are pure filesystem reads; every
subprocess repair is timeout-guarded; the pass runs on a blocking thread and
holds no lock a sweep needs.

```json
{
  "autonomous": {
    "installSelfCheck": {
      "enabled": false,
      "intervalSecs": 1800,
      "repair": false,
      "tokenRankingMaxAgeSecs": 3600
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_INSTALL_SELF_CHECK` | `autonomous.installSelfCheck.enabled` | env > config > default | `false` (off) |
| `LOOM_INSTALL_SELF_CHECK_INTERVAL_SECS` | `autonomous.installSelfCheck.intervalSecs` | env > config > default | `1800` (30 min) |
| `LOOM_INSTALL_SELF_CHECK_REPAIR` | `autonomous.installSelfCheck.repair` | env > config > default | `false` (report-only) |
| `LOOM_INSTALL_SELF_CHECK_RANKING_MAX_AGE_SECS` | `autonomous.installSelfCheck.tokenRankingMaxAgeSecs` | env > config > default | `3600` (1 h) |

See `loom-daemon/src/install_self_check.rs`.

### Primary-checkout reaper (#5268)

**The problem.** The merged-PR worktree reaper above and `loom-daemon clean`
both operate on `.loom/worktrees/<n>` entries — never on the **primary
checkout's own `HEAD`**. Nothing returns that checkout to the default branch,
so a primary clone left checked out on a feature branch (no PR ever opened) or
on a PR branch whose PR was closed without merging stays there indefinitely.
Every primary-clone agent — role ticks, the work finder, anything not running
inside a `.loom/worktrees/` worktree — then reads stale files off that branch,
and any repo-level `git` operation run from the primary checkout lands on the
dead branch by default. Observed during loom#5184 (2026-08-04): two of eight
managed-repo primary checkouts were parked exactly this way.

**What it does.** Rides the same per-registered-root tick as the worktree and
orphaned-process reapers above. For each root's primary checkout: resolves the
current branch and the repo's default branch (`origin/HEAD`); if the two
already match, does nothing. Otherwise, in order: skips a dirty tree (checked
via `git status --porcelain` **plus** a scan for `.git/{rebase-merge,
rebase-apply,MERGE_HEAD,CHERRY_PICK_HEAD,BISECT_LOG,REVERT_HEAD}`, so a rebase
paused between conflict-free steps still counts as dirty even though porcelain
status alone would read clean); probes the branch's PR state via the forge API
(REST-first, GraphQL fallback — the same `check_pr_status_for_branch[_rest]`
helpers the merged-PR worktree reaper's `check_pr_merged[_rest]` are built on,
never `git branch -d` reachability classification, which loom#4889 already
showed is unsafe under squash-merge); and — regardless of PR state — skips if
the branch carries any commit not yet safe to discard (ahead of its own
`@{u}` remote upstream when one is configured, or ahead of
`origin/<default>` for a branch that was never pushed at all). Only when every
gate passes does it `git checkout` the default branch, logging what it did.

**Why the unpushed-commits gate exists independent of PR state.** A `Merged`
verdict from the forge alone is not sufficient: a local branch can carry
commits made *after* what the PR merged that were never pushed, and comparing
`HEAD` against the default branch directly (instead of against the branch's
own upstream) would misclassify a squash-merged branch's already-merged
commits as "unpushed" — the same #4889 trap. Comparing against `@{u}` instead
answers "is there anything on this branch that isn't already on some remote
copy of it?", which is the actual question that matters for not losing work.

**Default-on**, for the same reason as the two reapers above: it restores a
tacit contract (agents read the default branch) whose absence is a
slow-motion outage.

```json
{
  "autonomous": {
    "primaryCheckoutReaper": {
      "enabled": true,
      "gracePeriodSecs": 600
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_PRIMARY_CHECKOUT_REAPER` | `autonomous.primaryCheckoutReaper.enabled` | env > config > default | `true` (on) |
| — | `autonomous.primaryCheckoutReaper.gracePeriodSecs` | config > default | `600` (10 min, same default as `worktreeReaper.gracePeriodSecs`) |

**Fails closed on every ambiguity.** A detached HEAD, no `origin/HEAD`, a
`git`/forge probe failure, or an indeterminate unpushed-commit count all skip
rather than act — under-acting here is recoverable on the next tick,
over-acting is not (this switches an operator's actual working tree, not a
disposable worktree directory). See `loom-daemon/src/primary_checkout_reaper.rs`.

**Abandoned-conflict detection (#6499).** Every pass also checks — before and
independent of the switch gates above — for unmerged index entries (`git
status --porcelain` `XY` in `DD`/`AU`/`UD`/`UA`/`DU`/`AA`/`UU`) with no
merge/rebase/cherry-pick actually in progress: the signature of an abandoned
`git stash pop` (or merge/cherry-pick) left unresolved, likely leaving live
`<<<<<<<`/`=======`/`>>>>>>>` conflict markers in a tracked file. When found,
it logs a specific `ERROR` (`ABANDONED CONFLICT STATE`, naming the path) on
every tick the condition persists — report-only, never a restore attempt;
this is the periodic counterpart to `check-main-clean.sh`'s own #6162 AC3
detection, which is Builder-workflow-invoked rather than periodic. See
[`troubleshooting.md` → Conflict markers left in `.loom/config.json` after a
`git stash pop`](troubleshooting.md#conflict-markers-left-in-loomconfigjson-after-a-git-stash-pop-6499).

### Autonomous periodic support-role runner (#4015)

Before this loop, the periodic **standalone** support roles — Champion,
Curator, Judge, Auditor, Guide — ran ONLY via GitHub Actions cron
(`.github/workflows/loom-*.yml`, Phase 2a of epic #3372/#3375), authenticating
with a single static `CLAUDE_API_KEY` secret with no rotation and no
health-awareness. Sweeps, by contrast, always ran host-side via
`sweep_registry`, drawing from the rotated, health-ranked token pool. That
split meant an operator provisioned *two* separate token systems for the same
underlying `claude -p "/role"` invocation, and a deployment with no
`CLAUDE_API_KEY` secret had its entire backlog-grooming pipeline silently dead
even while sweeps ran fine on the rotated pool.

**Scope.** This targets the *standalone* periodic roles — the ones the GitHub
Actions cron table below lists, plus `doctor` (added by #5272, see below). The
*per-sweep* lifecycle roles (Judge/Doctor/Champion-merge dispatched **inside**
a `/loom:sweep`) already run host-side on the rotated pool via
`sweep_registry` and are unaffected by this loop.

**Doctor is dual-mode (#5272).** Every other role in `DEFAULT_ROLES` is
*either* per-sweep (Builder) *or* standalone (this loop) — never both. Doctor
is the exception: `sweep_registry` still dispatches it per-sweep, inside a
live `/loom:sweep`'s judge-rejection loop, for a PR that sweep owns; this loop
*additionally* dispatches it standalone (`/loom:doctor` with no PR number,
exercising the role's own "Finding Work" queue scan rather than "PR Fix
Mode") as the periodic owner of any `loom:changes-requested` PR whose sweep
has already ended — crashed, exhausted its token, or spent its retry budget.
Before #5272 that state had no owner at all: Doctor only ever ran inside a
live sweep, so a PR left at `loom:changes-requested` once its sweep exited
was permanently parked. The two dispatch paths cannot race on the same PR:
this loop's own `(root, "doctor")` in-progress guard serializes concurrent
standalone ticks, and `doctor.md`'s own `loom:treating` claim + staleness
check (`LOOM_STALE_TREATING_MINUTES`) — unchanged by #5272 — serializes
against a *concurrent per-sweep* Doctor the same way it already serializes
against a concurrent standalone one. Doctor has no GitHub Actions cron
fallback (the table below never included it); its only "standalone" path is
this loop.

**What it does.** Per enabled role, on its own cadence (defaults mirror the
commented-out `cron:` schedules in `.github/workflows/loom-*.yml`: champion
10m, curator 5m, judge 5m, doctor 5m, auditor 10m, hermit 10m, guide 15m; plus
`architect` at 1h, idle-addressable-only), the daemon shells out to
`spawn-claude.sh -p "/<role>" --dangerously-skip-permissions` in the target
workspace — the identical launcher `sweep_registry` uses for sweep children —
so the role draws a token via the same 3-tier selection (ranking → allowlist →
random) and appears in the same `.loom/tokens/.bad_tokens` / `.ranking`
accounting as sweeps. Output is appended to
`.loom/logs/role-<role>.log`.

**Opt-in, unlike the token-ranking refresh above.** Each tick is a full
`claude -p` session that can mutate issues/PRs on the forge (dispatch-affecting
side effects, like the work finder / main-health gate), so an absent config
leaves the daemon's behavior byte-for-byte unchanged:

```json
{
  "autonomous": {
    "roleRunner": {
      "enabled": true,
      "roles": ["champion", "curator", "judge", "doctor", "auditor", "guide", "hermit"],
      "intervalSecs": 300,
      "onIdle": ["champion"]
    }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_ROLE_RUNNER` | `autonomous.roleRunner.enabled` | env > config > default | `false` (off) |
| `LOOM_ROLE_RUNNER_INTERVAL_SECS` | `autonomous.roleRunner.intervalSecs` | env > config > default | per-role built-in (see above) |
| `LOOM_ROLE_RUNNER_MAX_CONCURRENT` | `autonomous.roleRunner.maxConcurrent` | env > config > default | the 7 interval-default roles (concurrent role-agent ceiling, #6102 — bounds the agents `workFinder.maxConcurrent` does not) |
| — | `autonomous.roleRunner.roles` | config only | the 7 interval-default roles (`architect` excluded, #5656) |
| — | `autonomous.roleRunner.onIdle` | config only | `[]` (none; may name any of the 8 shipped roles, `architect` included) |
| — | `autonomous.roleRunner.model` | config only (`roleRunner.model` > `autonomous.model` > default) | `sonnet` (`DEFAULT_DISPATCH_MODEL`) |
| `LOOM_ARCHITECT_MAX_PROPOSALS` | `autonomous.roleRunner.architectMaxProposals` | env > config > default | `5` (per-invocation architect proposal cap, #5656) |
| `LOOM_ROLE_RUNNER_DETECT_COLLISIONS` | `autonomous.roleRunner.collisionDetection` | env > config > `autonomous.collisionDetection.enabled` > default | `false` (off) |
| `LOOM_ROLE_RUNNER_COLLISION_WINDOW_SECS` | `autonomous.roleRunner.collisionWindowSecs` | env > config > default | that role's tick interval, clamped to `[60, 3600]` |
| `LOOM_ROLE_RUNNER_SHARD_INDEX` | `autonomous.roleRunner.shardIndex` — **host-local tiers only**; a value in the *tracked* `.loom/config.json` is refused, not honored (#6374) | env > config > default | *(unset ⇒ unsharded)* |
| `LOOM_ROLE_RUNNER_SHARD_COUNT` | `autonomous.roleRunner.shardCount` | env > config > default | *(unset ⇒ unsharded)* |

The last two rows are the role runner's half of the cross-host collision
baseline — detection only, opt-in, and fully described under
[Cross-host role-tick collision detection (#4623)](#cross-host-role-tick-collision-detection-4623).

**Role children are model-pinned (#4501).** Every role spawn appends an explicit
`--model <resolved>` (immediately after the prompt, mirroring sweep dispatch), so
a scheduled role never inherits the selected account's *interactive* CLI default.
Before this, a host whose CLI default was `fable` ran every curator/champion/judge
tick on the most constrained quota tier and then died instantly on "You've reached
your Fable 5 limit". Resolution reuses `resolve_dispatch_model` — the same chain
sweeps use, including the #3982 logical-tier aliases (`opus` → `claude-opus-5`) —
with `autonomous.roleRunner.model` occupying the explicit-request tier. Blank
values are treated as unset at every tier (`--model ""` is never emitted). The
resolved model and the tier that supplied it are recorded in the per-role log
header: `==== loom-daemon role_runner: <ts> role=<role> model=<m> (source=<tier>) ====`.

**A pinned model can still be the wrong provider family for the admitted
runtime (#5028).** `runtimes.roles.<role> = "codex"` with no matching
`autonomous.roleRunner.roleModels.<role>` override resolves the Claude-shaped
default model above and forwards it to the Codex adapter, which 400s — before
#5028 this retried identically, at full cost, on every tick forever. Runtime
admission now resolves before the model, and a confidently-known
Claude-vs-Codex mismatch is refused pre-spawn as `RoleTickOutcome::
ModelRuntimeMismatch` instead of `Failure`: it never joins the generic failure
tally, `loom-daemon health`'s `roles` section names the broken config key
directly via the outcome's `detail()` (no spawn transcript needed), and the
per-root log warns once on the edge and downgrades to `DEBUG` on repeat. Full
mechanism and the `spawn-codex.sh`-side counterpart:
[`runtime-adapters.md` § "Model/runtime mismatch refusal (#5028)"](runtime-adapters.md#model-runtime-mismatch-refusal-5028).

`roles` restricts the dispatched subset (an explicit empty array runs none;
unknown names are ignored with a warning). **It is an allowlist, not an
addition** — a repo that pins it must add every newly-shipped default role by
name or that role silently never dispatches there (#5339, the reason
`doctor` joining `DEFAULT_ROLES` in #5272/#5291 stayed inert on this very
repo until its own `roleRunner.roles` was updated to include it). To catch
that class of staleness instead of failing silently, the multi-workspace role
loop warns about every **interval-default** `DEFAULT_ROLES` entry missing from
a **non-empty** pinned `roles` list (an explicit `[]` is a deliberate "run
none" opt-out, not staleness, so it stays quiet; and an idle-addressable-only
entry like `architect` is *correctly* absent, so it is never reported). Since
#6163 that warning is **one aggregated line that names the workspace whose
config was read**, listing every missing role at once, and it is emitted by a
single designated role loop **once per resolved-config change per workspace**
— not once per role per workspace per tick, which on a 25-workspace host
produced ~10,000 workspace-anonymous WARN lines per day. Roles covered by
`onIdle` are excluded from it: they dispatch on the idle edge by design, so
reporting them as "will not be dispatched" was misleading. `intervalSecs` — both the
env var and the config key — is a single override applied *uniformly* to
every enabled role's cadence; per-role cadence diversity otherwise comes from
each role's own built-in default.

#### Per-role built-in cadences, and reading the boot log (#6204)

The built-ins live in `role_runner.rs`'s `DEFAULT_ROLES` table and are pinned by
a unit test, so this list and the code cannot drift apart silently:

| Role | Built-in interval | Interval-default? |
|------|------------------|-------------------|
| `curator` | 300s (5 min) | yes |
| `judge` | 300s (5 min) | yes |
| `doctor` | 300s (5 min) | yes |
| `champion` | 600s (10 min) | yes |
| `auditor` | 600s (10 min) | yes |
| `hermit` | 600s (10 min) | yes |
| `guide` | 900s (15 min) | yes |
| `architect` | 3600s (1 h) | **no** — idle-addressable-only (#5656) |

At startup each spawned loop logs one line naming both the resolved cadence and
the tier that supplied it:

```
role_runner: curator interval=300s source=built-in (RoleSpec::default_interval_secs)
role_runner: curator interval=1800s source=env:LOOM_ROLE_RUNNER_INTERVAL_SECS (uniform override; per-role built-in 300s not used)
role_runner: curator interval=1800s source=config:autonomous.roleRunner.intervalSecs from private/shared defaults (/path/to/defaults.json) (uniform override; per-role built-in 300s not used)
```

**A uniform interval across all eight roles is the signature of an override,
not of the built-ins having stopped applying** — the exact misreading #6204 was
filed on. Two traps make it easy to conclude "no override is set" when one is:

- **The env var is read from the *daemon's* process environment**, not your
  shell's. On a launchd/systemd host it typically comes from the service
  definition (`~/Library/LaunchAgents/com.rjwalters.loom-daemon.plist`
  `EnvironmentVariables`, or the unit's `Environment=`), so `env | grep
  LOOM_ROLE_RUNNER_INTERVAL_SECS` in an operator shell shows nothing while the
  daemon has it. Check the service definition, or the `source=` field.
- **The config key resolves through the whole tier chain**, so it can come from
  a private/shared defaults file rather than the repo's committed
  `.loom/config.json`. The `source=` field prints the winning tier's path.

Interval resolution is the one role-runner knob read from the **daemon's own
home workspace** (`LOOM_WORKSPACE`) at startup rather than per-registered-root
per-tick: the loops' tickers are created once, so changing `intervalSecs`
requires a daemon restart (unlike `enabled`/`roles`/`onIdle`/`model`, which are
re-read every tick).

### Idle-addressable-only roles: `architect` (#5656)

`DEFAULT_ROLES` carries one entry that the interval cadence's *default* set
deliberately excludes: **`architect`**. `RoleSpec` has an `interval_default`
flag (`true` for the seven roles above, `false` for `architect`), and
`resolve_roles()`'s "unset `roles` ⇒ all defaults" fallback returns only the
`interval_default` subset. `resolve_on_idle_roles()` matches against the whole
table, so `onIdle: ["architect"]` resolves normally.

The asymmetry is the point. Before #5656 `architect` was absent from the table
entirely, so naming it in `roles`/`onIdle` was silently discarded with a "not a
known standalone role" warning — and since every other admitted role either
*processes* existing work (champion/curator/judge/doctor) or reacts to an
existing artifact (hermit to code, auditor to a build), a repo whose backlog
emptied had **no mechanism to acquire more**. But architect is a proposal
*generator*: adding it as a plain interval default would have put a speculative
proposal engine on a timer in every repo that never pinned `roles`, flooding
backlogs faster than Champion can triage. The idle edge is the right trigger
because it is self-throttling by construction — a repo with work never fires it
— and it is exactly the "this repo has run out of work" condition.

```json
{
  "autonomous": {
    "workFinder": { "enabled": true },
    "roleRunner": {
      "enabled": true,
      "onIdle": ["architect"],
      "architectMaxProposals": 5
    }
  }
}
```

A repo that genuinely wants a timer-driven architect opts in explicitly by
naming it in `roles` (1h default cadence). Both paths pass the resolved
per-invocation cap through as `/loom:architect --max-proposals <n>`; see
`architectMaxProposals` in the config table above.

`onIdle` (#4364) lists the subset of the shipped roles to *also* fire on the
work-finder **idle edge** — the moment a workspace transitions from busy to
idle, defined per-root as a post-tick `in_flight().is_empty()` (0 in-flight
sweeps AND nothing dispatched that tick). This composes with (never replaces)
the interval cadence: the interval loop remains the backstop for hosts that are
never idle. It is **edge-triggered, not level-triggered** — a queue that stays
empty across many ticks fires at most once, and a daemon that boots on an empty
queue does not fire — plus a min-60s debounce per (root, role) against rapid
idle/busy flapping, and a shared in-progress guard so an idle run never overlaps
that role's interval run (or another idle run). Absent → no idle triggering (the
opposite default from `roles`); unknown names are ignored with a warning; a
scheduled drain suppresses it. Because the work finder is the sole source of the
idle signal, `onIdle` is **inert unless the work finder is enabled** — the daemon
logs a one-time startup warning if it is set with the work finder off. The
motivating case is `["champion"]`: an idle daemon usually means the approved
queue just drained, and champion promotion (`loom:curated` → `loom:issue`) is
exactly what refills it, closing the promote → dispatch loop in seconds instead
of waiting out the rest of a fixed interval.

**GitHub Actions workflows remain a supported fallback** for deployments with
no always-on daemon — this loop does not remove them, it gives an always-on
daemon host a better primary path. Running both simultaneously is harmless
(each is an independent `claude -p "/<role>"` invocation against the shared
forge state; the state machine's label-based coordination is what prevents
double-work, not a single dispatcher).

**Never fatal, first tick skipped.** A failed invocation (script missing,
non-zero exit, timeout — role invocations are killed via their process group
after a generous default 30-minute timeout, mirroring `sweep_registry`'s
group-signal cancel path) is logged and skipped; the next tick tries again.
Unlike the read-only token-ranking refresh, this loop skips its first tick
(mirrors the work finder / main-health gate) so several role loops starting at
daemon boot don't burst several `claude` sessions at once.

**Multi-workspace.** Like the other autonomous loops, each role's task
re-reads the workspace registry every tick and dispatches into every
registered repo that has that role enabled in its own config (an empty
registry reduces to the single daemon workspace). See
`loom-daemon/src/role_runner.rs` for the implementation.

**`enabled` is resolved per root, not inherited (#4377).** `autonomous.roleRunner.enabled`
(and `roles` / `onIdle`) is read from **each registered workspace's own**
`.loom/config.json` — both the interval loops above and the `onIdle` idle-edge
path call the identical `resolve_enabled(read_role_runner_config(root))` for
`root`. The daemon workspace's own `autonomous.roleRunner.enabled` only decides
whether these loops **start at all**; it does not propagate to the other
workspaces `loom-daemon workspace add` registers. A registered workspace with
no `autonomous` block of its own is therefore role-runner-disabled by default —
including for `onIdle` — even when the daemon's own workspace has
`enabled: true`. Set `autonomous.roleRunner.enabled: true` in **that root's
own** config to opt it in.

Before #4377 this per-root gate was silent: a disabled root's interval skip
logged only at `debug!` (invisible at the default `info` level), and the
`onIdle` bail logged nothing at any level. Now:

- The interval loop logs a one-time `warn!` per root the first tick it sees
  that root disabled (further identical ticks downgrade to `debug!`, mirroring
  the `missing_roots_warned` dedup from #4326); the warning clears — and can
  fire again — once the root re-enables and later disables again.
- The `onIdle` path logs a one-time `warn!` on the first idle edge it observes
  for a root that has `onIdle` roles configured but is disabled — the exact
  silent no-op this issue fixes. A root with no `onIdle` configured stays quiet
  when disabled: that is its normal, unconfigured state, not a
  misconfiguration.
- `loom-daemon status` shows each registered root's resolved role-runner
  state in the "Managed repos" table's `ROLES` column (`on`/`off`), plus an
  explicit note when a disabled root has `onIdle` roles configured — so the
  "N of M registered workspaces are inert" state is diagnosable without
  reading every root's config file by hand.

#### Bringing a new repo online (runbook, #4498)

`loom-daemon workspace add` registers a repo for *dispatch*. It does **not** give
that repo a **queue** — no role runner means no Curator, Judge, Champion, Auditor,
or Guide runs there, so nothing ever reaches `loom:issue` and dispatch has nothing
to pick up. A newly-registered root therefore shows `ROLES off` in
`loom-daemon status` and sits inert indefinitely, looking registered-and-healthy.
The steps below are what turns a registered root into a *self-feeding* one.

**1. Choose a config surface.** `autonomous.roleRunner.enabled` is read from the
root's own **effective** config, which `config_resolver::resolve_effective_config`
(`loom-daemon/src/config_resolver.rs:143-153`) deep-merges from four tiers,
lowest → highest priority:

| # | Tier | Path | Tracked? |
|---|------|------|----------|
| 1 | machine-level defaults | `~/.local/share/loom/config/defaults.json` (override: `LOOM_CONFIG_DEFAULTS_FILE`; set it empty to disable the tier) | no — outside every checkout |
| 2 | legacy per-repo | `<root>/.loom/config.json` | yes |
| 3 | project | `<root>/.loom-project/project.json` | yes (Epic #3835 Phase 6) |
| 4 | host-local | `<root>/.loom-local/local.json` | no (git-ignored) |

Two valid ways to flip a root on, with very different blast radii:

- **Machine-level (tier 1)** — one edit per *host* turns on **every** registered
  root that does not set `roleRunner.enabled` itself. This is the right lever for
  a fleet of repos you own that should all behave the same way. Because it is the
  lowest-priority tier, any repo that wants to opt back out can simply set
  `enabled: false` in its own config and win.
- **Per-repo (tier 2/3/4)** — one edit per *repo*, in that repo's own checkout.
  Use this when a root needs a different `roles`/`onIdle` set than the host
  default, or when the repo's maintainers should see the setting in review.

Either way the JSON block is the same shape as the example above:

```json
{
  "autonomous": {
    "roleRunner": {
      "enabled": true,
      "roles": ["champion", "curator", "judge", "doctor", "auditor", "guide", "hermit"],
      "intervalSecs": 300,
      "onIdle": ["champion"]
    }
  }
}
```

Remember `onIdle` is **inert unless the work finder is enabled** for that root,
and that `LOOM_ROLE_RUNNER` (env) outranks all four config tiers. To verify a
root's `onIdle` roles are actually firing rather than merely configured, run
[`check-onidle-status.sh`](../scripts/check-onidle-status.sh) — it cross-checks
the config against the daemon log's `idle edge … firing idle-triggered <role>
run` line. See [onidle-phase3-finding.md](onidle-phase3-finding.md) for this
repo's own onIdle activation history and the Epic #5038 Phase 3 Class-3-residue
finding.

**2. Sync the repo's labels.** The whole coordination protocol is label-based, so
a repo with no `loom:*` labels cannot hold a queue — a Curator that tries to apply
`loom:curated` just fails. Since every GitHub label operation is a `gh label` API
call, this needs **no checkout of the target repo**: run
`sync-labels.sh --repo OWNER/NAME` from a workspace that already has an
authoritative `.github/labels.yml` (labels.yml is read locally; only the target
moves). This is additive by default (#5066) — it only creates/updates the Loom
labels and never touches GitHub's default labels (`bug`, `enhancement`, etc.),
so it is safe to run against a repo that predates Loom without first previewing.
Preview anyway if in doubt:

```bash
# From a checkout whose .github/labels.yml is the source of truth (e.g. loom):
for r in OWNER/tool-repo \
         OWNER/analog-a OWNER/analog-b OWNER/analog-c \
         OWNER/analog-d OWNER/analog-e OWNER/analog-f \
         OWNER/analog-g; do
  ./.loom/scripts/sync-labels.sh --repo "$r" --dry-run   # preview
done
# ...then re-run without --dry-run to apply.
```

`--dry-run` is deliberately forge-free (it reports intent from labels.yml alone
and makes zero `gh` calls), so it also validates the parse and the NWO offline.
`--repo` is GitHub-only — an explicitly configured Gitea forge rejects it,
whether that comes from `LOOM_FORGE_TYPE=gitea` or `forge.type: gitea` in any
config tier. For a Gitea root, run `sync-labels.sh` from a checkout of that
repo so the Gitea helpers can resolve a base URL and token.

If a genuinely greenfield repo also needs GitHub's default labels cleared, add
`--prune-defaults` (the old, pre-#5066 unconditional-deletion behavior). Because
`--repo` points that deletion at a repo you are not standing in, a typo'd NWO is
worth catching first — pair it with `--dry-run`, which for `--prune-defaults`
additionally performs a read-only in-use lookup so the preview flags which
default labels are still attached to an issue/PR. The **real** `--prune-defaults`
run preflights the named target with `gh repo view` before deleting anything and
aborts if it is missing or read-only (#4524); it then skips (with a warning
listing the affected issue/PR numbers) any default label still in use, unless
`--force` is also given.

**3. Refresh a stale install.** A root installed before the machine-level daemon
model carries a committed file-copy of `.loom/` that **shadows** the machine
checkout and drifts stale — including the role prompts the role runner invokes.
Run `loom migrate --dry-run` then `loom migrate` in that root (see
`defaults/docs/machine-dispatcher.md` → `loom migrate`); for a still-file-copy
install that is not being migrated yet, `./.loom/scripts/resync-installed.sh`
refreshes the copies in place.

A repo on the **v0.15 multi-repo canary vintage** predates a cluster of
multi-repo fixes. Rule these out before blaming a new symptom on config — several
of them present exactly as "registered, enabled, and still nothing happens":

| Issue | Symptom if the install predates the fix |
|-------|------------------------------------------|
| #3936 | `loom-daemon status`'s token-capacity summary contradicts its own per-token table |
| #3937 | sweep-registry label flips run `gh issue edit` unscoped — cross-repo claims fail, yet dispatch proceeds *unclaimed* |
| #3938 | dispatched sweeps die in ~2s: no `.loom/tokens/` pool outside the primary workspace (fixed by the shared machine-level pool — see "Token pool provisioning for managed repos" above; bootstrap it once per host) |
| #3939 | no backoff/quarantine for insta-crashing dispatches — the same few issues are re-dispatched every tick, starving the rest of the queue |

**4. Verify.** In order, because each step's failure looks like the next one's:

1. `loom-daemon status` — the new root appears in "Managed repos" with
   `ROLES on`. Still `off` means the config edit did not land in a tier that root
   actually reads (tier 1 is per-*host*: check `LOOM_CONFIG_DEFAULTS_FILE` is not
   set to something else, and that the daemon process's `$HOME` is the one you
   edited). A restart is not required — each role task re-reads the registry and
   per-root config every tick.
2. Within one Auditor/Curator interval (default 300s), expect **≥1 issue labeled
   `loom:triage`** in that repo — the Auditor files what it finds, the Curator
   enriches it. An empty queue after two intervals with `ROLES on` means the role
   invocation itself is failing; role failures are never fatal and are logged +
   skipped, so read the daemon log rather than waiting.
3. Then, and only then, expect dispatch: `loom:issue` → a sweep → a PR.

> **Caveat (#4501).** A dispatched child that dies immediately — e.g. on a
> `Fable 5 limit` classified as non-retryable, where dispatch inherited the
> interactive model default instead of pinning a work model — is
> indistinguishable at the forge from "the role runner is still off": both look
> like a repo with no activity. Confirm step 1's `ROLES on` and step 2's
> `loom:triage` issue *before* concluding anything about dispatch, and treat an
> insta-dying child as #4501 rather than a config problem.

**Which steps an agent cannot do for you.** Steps 1, 3, and 4 are operator steps,
run by hand on **each daemon host** — repeat them per host, since tier 1 is
per-machine and a multi-host fleet has one copy of it per machine:

- **Step 1 (config edit)** and **step 3 (install refresh)** are host-local
  filesystem writes: a file in the operator's home directory, or another repo's
  own checkout. Neither is reachable from a Builder worktree — the
  worktree-isolation guard confines Edit/Write to the current worktree, and the
  machine-level defaults file lives outside every git checkout entirely.
- **Step 4 (verification)** needs `loom-daemon status` against the *running*
  daemon on that host, plus a real Auditor/Curator cycle to elapse. An agent can
  read the forge state but cannot observe another host's daemon.
- **Step 2 (label sync)** is the exception: `--repo` makes it pure forge API, so
  it runs from any checkout with `gh` auth for the targets — one loop covers the
  whole target set at once.

### Durable operator watches (#3971)

**Problem it fixes.** An operator armed a 4-hour background poll watching two
items for terminal state, then their Claude Code session crashed and the watch
died with it — background tasks are children of the session process, so the
terminal-state report was silently lost. The root cause is harness behaviour,
but the durable fix belongs on the daemon: it is the long-lived process that
already outlives any one operator session.

**What it does.** An operator registers a watch on an issue or PR
(`register_watch` MCP tool, or `loom-daemon watch add`). The watch is persisted
machine-level to `~/.loom/watches.json` (override `LOOM_WATCHES_PATH`) so it
survives **both** the registering session's death **and** a daemon restart. A
default-on monitor loop polls each watch's terminal state on a cadence and, when
a watch reaches a terminal state or expires, **durably appends** a JSON line to
`~/.loom/logs/watch-results.log` (override `LOOM_WATCH_RESULTS_LOG`) — a file a
later session can trivially `tail` — then drops the watch from the active set.

**Terminal states** (forge-observable, read via `gh issue view` / `gh pr view
--json state,labels`):

| Outcome | Condition |
|---------|-----------|
| `closed` | issue/PR state `CLOSED` |
| `merged` | PR state `MERGED` |
| `blocked` | still-open item carrying the `loom:blocked` label |
| `expired` | monitor-generated — no terminal state within the expiry window (bounds the watches file + forge-call budget) |

**Why forge polling, not the event bus.** The in-memory event bus
(`sweep.issue.{N}.*`, `sweep.global.*`) only knows about sweeps *this daemon
dispatched*, and the motivating case is explicitly cross-repo (a `vibesql` issue
watched from the `loom` operator session) which the daemon may never have swept.
The v0.10.0 event taxonomy is also frozen. So the monitor **polls the forge
directly**, which works uniformly for any repo — sweep-backed or not — **without
minting a new event topic**. Address the target cross-repo with either `repo` (a
forge slug `owner/name`, preferred) or `workspace_root` (the `gh` query runs in
that repo's working dir — the same addressing pattern as `dispatch_sweep` /
`list_sweeps`).

**Default-on, like the token-ranking refresh.** It has no dispatch side effect
and makes **zero** forge calls until an operator registers a watch (an empty
registry short-circuits the tick to a single file read). Full opt-out knob:

```json
{
  "autonomous": {
    "watchMonitor": { "enabled": true, "intervalSecs": 120, "expirySecs": 86400 }
  }
}
```

| Env var | Config key | Precedence | Default |
|---------|-----------|------------|---------|
| `LOOM_WATCH_MONITOR` | `autonomous.watchMonitor.enabled` | env > config > default | `true` (on) |
| `LOOM_WATCH_MONITOR_INTERVAL_SECS` | `autonomous.watchMonitor.intervalSecs` | env > config > default | `120` (2 min) |
| `LOOM_WATCH_MONITOR_EXPIRY_SECS` | `autonomous.watchMonitor.expirySecs` | env > config > default | `86400` (24h; `0` disables expiry) |

**Never fatal.** A missing/corrupt watches file degrades to an empty registry; a
single failing probe (network, `gh` missing, transient forge error) is logged
and the watch retained for the next tick rather than aborting the whole tick.

**IPC / CLI surface.**

| IPC request | MCP tool | CLI | Purpose |
|-------------|----------|-----|---------|
| `RegisterWatch` | `register_watch` | `loom-daemon watch add <N> [--pr] [--repo O/N] [--note …]` | Register a durable watch (idempotent on `(target, kind, number)`) |
| `ListWatches` | `list_watches` | `loom-daemon watch list [--json]` | List active watches |
| `RemoveWatch` | `remove_watch` | `loom-daemon watch remove <id>` | Remove a watch by id |

See `loom-daemon/src/watch_registry.rs` for the implementation.

### Safe start / stop (raw daemon process)

`.loom/bin/loom start|stop` manage the **tmux Manual-Orchestration-Mode pool** —
a different process model from the `loom-daemon` binary that hosts the
work-finder / health-gate loops. Two dedicated wrappers manage the raw daemon
process:

```bash
# Plain start = FLAGS-OFF reliability daemon: BOTH autonomous loops OFF, no
# auto-dispatch. This is the safe default (#3911), consistent with the
# ecosystem-wide opt-in / default-off contract (LOOM_WORK_FINDER unset => off,
# LOOM_MAIN_HEALTH_GATE unset => off, precedence env > config > default):
./.loom/scripts/cli/loom-daemon-start.sh

# Opt in to autonomous loops explicitly:
./.loom/scripts/cli/loom-daemon-start.sh --work-finder   # work finder on
./.loom/scripts/cli/loom-daemon-start.sh --health-gate   # main-health gate on
./.loom/scripts/cli/loom-daemon-start.sh --work-finder --health-gate   # both on

# Enable strictly per .loom/config.json → autonomous (no env forcing):
./.loom/scripts/cli/loom-daemon-start.sh --from-config

# --from-config COMPOSES with an explicit flag (#4353): the named loop is
# forced, the other is left for config to drive:
./.loom/scripts/cli/loom-daemon-start.sh --from-config --work-finder      # force finder ON, gate config-driven
./.loom/scripts/cli/loom-daemon-start.sh --from-config --no-health-gate   # force gate OFF, finder config-driven

# Explicit-off / foreground variants:
./.loom/scripts/cli/loom-daemon-start.sh --no-work-finder   # force finder off (explicit; same as default)
./.loom/scripts/cli/loom-daemon-start.sh --no-health-gate   # force gate off (explicit; same as default)
./.loom/scripts/cli/loom-daemon-start.sh --foreground       # run attached, no PID file

# Clean shutdown:
./.loom/scripts/cli/loom-daemon-stop.sh            # SIGTERM → grace → SIGKILL
./.loom/scripts/cli/loom-daemon-stop.sh --force    # immediate SIGKILL
```

`loom-daemon-start.sh`:
- **defaults FLAGS-OFF (#3911)**: a bare invocation exports `LOOM_WORK_FINDER=0`
  and `LOOM_MAIN_HEALTH_GATE=0`, so a plain start is a **reliability daemon** that
  does **not** auto-dispatch sweeps — consistent with the ecosystem-wide opt-in /
  default-off contract. An already-exported env var always wins; `--work-finder`
  / `--health-gate` force the respective loop on; `--from-config` alone leaves
  both unset so `.loom/config.json → autonomous` drives (precedence env >
  config > default). **`--from-config` composes with an explicit flag rather
  than ignoring it (#4353)**: `--from-config --work-finder` (or
  `--no-work-finder`/`--health-gate`/`--no-health-gate`) still FORCES that one
  var while leaving the other loop unset for config to drive — a
  pre-exported env var still wins over the forced flag,
- locates the `loom-daemon` binary (`LOOM_DAEMON_BIN` → `PATH` → `target/{release,debug}`),
- runs the **advisory** host-sleep check (`check-host-sleep.sh`, #3350) — never blocks the start,
- **on macOS, backgrounds the daemon as a launchd LaunchAgent** in the resolved
  per-user domain (`gui/<uid>` with an active GUI login, else the SSH-reachable
  `user/<uid>` background domain — #4130) instead of a plain `nohup … &` (#3972 —
  see "macOS session-bootstrap hazard" below); **on a systemd Linux host,
  installs + enables a `systemd --user` service** (#4268 — see "systemd user unit
  (Linux)" below) that mirrors the launchd supervision contract
  (`Restart=on-success`, disable-on-stop, `LOOM_DAEMON_SUPERVISOR=systemd`); on a
  non-systemd Linux host (or with `--no-systemd` / `LOOM_DAEMON_SYSTEMD=0`) it
  stays a plain nohup background job,
- backgrounds the daemon and writes a PID file at `.loom/.daemon.pid` (gitignored)
  — or, in **machine mode** (dispatcher-driven, `LOOM_MACHINE_CHECKOUT` set,
  #4229), at `$HOME/.loom/.daemon.pid` so the same machine-wide launchd
  singleton is tracked consistently no matter which repo `loom start` ran
  from; see [`machine-dispatcher.md`](machine-dispatcher.md#the-pidflags-relocation-decision).
  Since **#4774 the daemon also writes this file itself**, immediately after its
  socket bind succeeds, to the path the start script exports as `LOOM_PID_FILE`.
  That is what keeps it correct across the relaunches this script is not part of
  — launchd `KeepAlive`, systemd `Restart=`, the `RestartDaemon` primitive, the
  self-update roll, `launchctl kickstart`. The write sits *after* the singleton
  guard's bind on purpose: a daemon the guard refuses must never overwrite the
  live incumbent's entry. `status` / `health` cross-check the recorded pid
  against `daemon_pid` (the answering process's own `std::process::id()`) and
  report a stale file rather than trusting it,
- refuses a second start when the PID file points at a live process, and surfaces
  the daemon's own **singleton-guard** refusal (#3806) — if the backgrounded
  process exits immediately it prints the startup-log tail instead of leaving a
  silently-dead process.

`loom-daemon-stop.sh` sends **SIGTERM** (not just Ctrl-C/SIGINT — the daemon now
handles both, #3813), waits `LOOM_DAEMON_STOP_GRACE_SECS` (default 10s), then
escalates to SIGKILL. On macOS it additionally `launchctl bootout`s the
LaunchAgent job definition once the process is confirmed dead (see below). On a
systemd Linux host it detects the `systemd --user` ownership
(`systemctl --user is-active`/`is-enabled`) and instead stops + **disables** the
unit (`systemctl --user disable --now`), so a subsequent reboot does not
resurrect it (#4268 — see "systemd user unit (Linux)" below).

**Shutdown decision — sweeps survive *on launchd*, they are not drained.** A clean
daemon stop removes the Unix socket and exits, but **does not cancel in-flight
`/loom:sweep` children**. On **launchd** those are independent detached processes
that reparent to `pid 1` and survive a daemon restart by design — killing the
dispatcher must not kill dispatched work — and the registry reconciles their state
on the next start (`SweepRegistry::reconstruct` re-admits live-lock owners).

> **Supervisor difference — on systemd, a plain stop/restart KILLS sweeps (#5119).**
> Under a `systemd --user` service the sweep/role children run **inside the service's
> cgroup**, so systemd's stop job signals them by construction when the daemon
> exits (`KillMode=mixed` SIGKILLs the remaining cgroup processes). The "sweeps
> survive by design" guarantee is therefore **launchd-only**; a plain
> `loom-daemon restart` on systemd terminates in-flight sweeps and role runs. Use
> `loom-daemon restart --drain` (below) — which empties the sweep registry before
> exiting so the cgroup is empty when the stop job runs — to preserve them. The
> restart primitive's ack message is supervisor-aware and states this plainly.

To actively cancel a sweep, use
`mcp__loom__cancel_sweep` against a running daemon *before* stopping it.

> **Amended by #4090 (scheduled drain-and-restart).** The above describes the
> *plain* stop/restart: sweeps survive the process boundary but the relaunched
> daemon's in-memory registry starts empty, so a plain `RestartDaemon` leaves the
> surviving children as **orphans** — invisible to `list_sweeps`, the concurrency
> cap, the watchdog, and the reaper, and still holding `loom:building` for a
> `claim_reconciliation` pass to re-dispatch as duplicates. When you want "finish
> what you started, then roll" instead, use `loom-daemon restart --drain` (below):
> it stops admitting new work and waits for the registry to empty before exiting,
> so no sweep is killed and no orphan is left behind.

**Exit code carries shutdown intent (#4054).** The daemon encodes *why* it is
shutting down in its exit code, because the launchd `KeepAlive:{SuccessfulExit:true}`
plist (below) relaunches only on a clean exit `0`: the `RestartDaemon` primitive
exits `0` (relaunch), SIGTERM exits `143`, SIGINT exits `130`, an explicit IPC
`Shutdown` exits `143`, and a crash/panic exits non-zero — so only a deliberate
restart trips a relaunch, and "an operator stop stays stopped" holds without
depending on `bootout` timing. `loom-daemon-stop.sh` also re-verifies (scoped to
its launchd label) that no daemon is still alive after the stop and exits non-zero
if one is, closing the silent-success hole where a failed bootout could leave a
relaunched daemon dispatching.

**Startup failures end the process directly (#4531).** A daemon that cannot start
— most visibly the singleton guard's "another loom-daemon is already listening …
— refusing to start" refusal (#3806) — prints that error to stderr and calls
`std::process::exit(1)` itself rather than returning `Err` out of `main`. The
message and the exit code are exactly what `Termination for Result` used to
produce; what changes is that termination is now *immediate*. Returning `Err`
made `#[tokio::main]` drop its `Runtime`, which blocks until every in-flight
`spawn_blocking` task finishes — so a refusing second daemon kept running its
already-spawned periodic loops for ~10s after announcing it would not start, long
enough to look like a hang under any short timeout.

### Fleet quiesce — stopping the daemon is NOT a fleet stop (#6129)

**The incident this closes.** On 2026-08-13, `loom-worker-2` (Ubuntu 24.04,
`systemd --user`) was drained before an operator-ordered instance stop.
`systemctl --user stop loom-daemon.service` reported success, but role agents
(Champion/Curator/Judge/Doctor/Guide) kept running and drawing on the token
pool — `pgrep -af "claude-wrapper.sh -p /loom:"` still showed them, fluctuating
7→6→10→6 rather than draining. The cause: `spawn-claude.sh`'s per-spawn
CPU-quota mechanism (#5111, default-on whenever a `systemd --user` manager is
reachable) wraps the final `claude`/`claude-wrapper.sh` exec in `systemd-run
--user --scope`, which creates a **transient scope owned by the `systemd --user`
manager itself** — a sibling unit, not a descendant of `loom-daemon.service`'s
own cgroup — so an ordinary stop has no parent/cgroup relationship through
which to reach it. This is architecturally the same shape as a launchd child
reparenting to `pid 1`: the agent survives the dispatcher's exit **by design**
(see the "sweeps survive by design" note above) — the gap #6129 closes is that
operators had no *deliberate, explicit* way to reap them as a group when they
actually want to.

**`loom-daemon-quiesce.sh` is that explicit action** — a single command that
(1) stops dispatch via `loom-daemon-stop.sh` and (2) enumerates and stops every
in-flight role/sweep child, the same way on launchd and systemd:

```bash
./.loom/scripts/cli/loom-daemon-quiesce.sh              # full quiesce
./.loom/scripts/cli/loom-daemon-quiesce.sh --dry-run     # resolve every target, mutate nothing
./.loom/scripts/cli/loom-daemon-quiesce.sh --force       # skip the grace window (SIGKILL immediately)
```

It never runs automatically and nothing else in this repo invokes it — a bare
`systemctl --user stop`/`launchctl bootout`/`loom-daemon-stop.sh` continues to
leave in-flight work running exactly as documented above; this is the
opt-in "actually drain the host" tool an operator reaches for when the ordinary
stop is not enough (host maintenance, cost, or — the incident's own trigger —
stopping a host from continuing to draw on an exhausted token pool). Its two
enumeration mechanisms:
- **Linux systemd --user**: every active `loom-agent-*.scope` unit — the
  predictable per-spawn naming (`loom-agent-<pid>-<rand>.scope`, grouped under
  `loom-agents.slice`) `spawn-claude.sh`'s CPU-quota wrap assigns as of #6129,
  replacing the opaque `run-r<hex>.scope` name systemd generates by default —
  via `systemctl --user stop`.
- **Every platform** (the *only* mechanism on launchd, which has no
  scope/unit construct): any process whose command line names a
  `claude`/`claude-wrapper.sh` binary AND a `-p /loom:` prompt flag — SIGTERM
  first, escalating survivors to SIGKILL after a grace window.

A bare `systemctl --user stop loom-daemon` (or the daemon's own SIGTERM
handler log, for a raw stop that never goes through `loom-daemon-stop.sh` at
all) now names this script explicitly, so an operator does not have to already
know the distinction to discover it.

**`failed` vs `inactive` on a clean systemd stop (#6129).** A clean operator
stop was also landing the `loom-daemon.service` unit in `failed`, not
`inactive` — `EXIT_SIGTERM`/`EXIT_SHUTDOWN` (143) and `EXIT_SIGINT` (130) are
non-zero, and `Type=simple`'s default "clean exit" criterion is exit `0` or
termination BY one of SIGHUP/SIGINT/SIGTERM/SIGPIPE, neither of which matches
a `std::process::exit(143)` *exit code*. `render_systemd_unit()` now sets
`SuccessExitStatus=143 130` (reclassifies both as a clean exit) paired with
`RestartPreventExitStatus=143 130` (belt-and-braces: vetoes `Restart=on-success`
firing on those codes regardless of *how* the process died, since
`SuccessExitStatus=` widens what a bare `kill -TERM` outside `systemctl`
would count as "clean" — see the rationale comment at the `printf` site in
`loom-daemon-start.sh` for the full systemd.service(5)-sourced argument).

**Known caveat, deliberately left unresolved here.** `restart_scheduled_message`'s (in the loom source repo's `loom-daemon/src/ipc.rs`)
systemd wording — "in-flight sweeps and role runs do NOT survive on systemd,
they run inside this service's cgroup" — is only true for a child this daemon
execs *directly*; a CPU-quota-wrapped scope child behaves like the launchd
case instead (survives, silently). Which shape applies to a given child is
invisible to the daemon (the wrapping decision is entirely inside
`spawn-claude.sh`, an opaque subprocess boundary), so the message is not
rewritten to guess — treat it as accurate for the *unwrapped* case only, and
use `loom-daemon-quiesce.sh` (which enumerates real scopes/processes instead
of asserting a platform-wide claim) when you need a definite answer either
way.

### Autonomy-loss watchdog + heartbeat (#4011)

**The failure this closes.** On 2026-07-26 the `loom-daemon` launchd job took a
SIGTERM two seconds after starting and was left `bootout`-ed (unloaded) from
launchd. Autonomous dispatch silently stopped, and **nothing surfaced it** — no
log line, no forge signal, no notification. It was discovered hours later only
because someone happened to run `loom-daemon status` by hand. `loom-daemon
status` is a *pull*, and that pull is exactly the thing that didn't happen.

The fix is a *host-side* detector that lives **outside** the daemon process — a
dead daemon cannot report its own death (which is why #3971's in-daemon watch
loop is not reusable as the reporter). It has three cooperating parts:

1. **Durable autonomy-desired marker** (`<loom_dir>/autonomy-desired`).
   `loom-daemon-start.sh` writes it on a successful start; **only** an
   operator-initiated `loom-daemon-stop.sh` removes it. Its lifetime is
   **operator intent**, not process liveness — deliberately, because both of the
   "obvious" markers (the pid file, the loaded launchd job) are destroyed by the
   very stop path that causes an outage, so a detector built on them could not
   tell a deliberately-stopped daemon from a silently-dead one. The marker
   records the paths + label the watchdog needs (heartbeat file, cadence,
   pid file, launchd label, `use_launchd`).

2. **Declared-cadence heartbeat** (`<loom_dir>/daemon.heartbeat`, `loom-daemon/src/daemon_heartbeat.rs`).
   The running daemon rewrites it every `intervalSecs` (default 60s). This is an
   *explicit* liveness contract, chosen over piggybacking on the token-ranking
   `.ranking` mtime (#3969) — that is a config-disableable side effect, so a
   detector keyed to it would silently stop working when someone turned that loop
   off. Default-on (read-only, no dispatch side effect); opt out with
   `LOOM_DAEMON_HEARTBEAT=0` / `autonomous.heartbeat.enabled=false`.

3. **Host-side watchdog** (`loom-daemon-watchdog.sh`), the payload of a
   **second, separate scheduled job** from the daemon job/unit itself, that
   `loom-daemon-start.sh` provisions on a recurring cadence (default 300s):
   - **Darwin**: a second **launchd job** (`<daemon-label>-watchdog`) on a
     `StartInterval` cadence.
   - **systemd Linux** (#4260 sub-issue D): a `Type=oneshot`
     `<daemon-unit>-watchdog.service` driven by a paired
     `<daemon-unit>-watchdog.timer` (`OnUnitActiveSec` + `OnBootSec`,
     `Persistent=false`), `enable --now`'d on the **timer** (mirroring
     `loom-daemon.service` itself — #4268).

   Each run compares intent (marker present?) against reality (daemon loaded +
   alive? heartbeat fresh?) and, on divergence, appends a loud line to
   `<loom_dir>/logs/daemon-watchdog.log` and stderr (which launchd/systemd both
   capture into the same log via the rendered job/unit's stdout/stderr redirect)
   — **and, since #5391, recovers**: see "The watchdog recovers, it is not a
   report-only detector" below.

**The watchdog recovers, it is not a report-only detector (#5391).** Through
#5118 the only automatic remediation was two deliberately narrow gates
(#4232 launchd / #4862 systemd) covering ONE signature: "job loaded + down +
last exit 0". Every other confirmed outage — a genuine crash, a booted-out job,
a dead pid with an unreachable socket — printed `Recover with:
loom-daemon-start.sh` and stopped there. One fleet host logged **252 such
`[DIVERGENCE]` lines in eight days**, including a continuous **1h40m** outage
that ended only because a human went looking. A detector that prints the exact
recovery command every five minutes and never runs it is, operationally, close
to no watchdog at all.

The naive fix (restart on every down tick) is worse: pointed at a genuinely
broken binary it becomes a restart loop that burns tokens and hides the fault.
So the policy is the middle path — **recover, bounded, then escalate loudly**:

| Bound | Behavior | Knob (default) |
|-------|----------|----------------|
| Attempt budget | at most N recovery attempts **per outage episode**, tallied durably across ticks | `LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS` (`5`) |
| Backoff | attempt N waits `base × 2^(N-1)` since the last attempt; ticks inside the window report but do not re-run | `LOOM_WATCHDOG_RECOVER_BACKOFF_SECS` (`60`), capped by `LOOM_WATCHDOG_RECOVER_BACKOFF_CAP_SECS` (`1800`) |
| Circuit breaker | once the budget is spent it **latches open** — no further automatic attempts until a tick observes a healthy daemon, or `<loom_dir>/.watchdog-recovery-state` is deleted | — |
| Never revives a stop | an operator-stop exit signature (launchd `last exit status` 143/130/-15/-2; systemd `ExecMainCode=killed` + TERM/INT) is reported, never restarted | — |
| Escalation | on breaker trip (or when recovery is structurally impossible and the outage has run N consecutive ticks) files **one** forge issue via `create-issue.sh`, deduped by `<loom_dir>/.watchdog-outage-escalated` | `LOOM_WATCHDOG_ESCALATE` (on) |

What it runs is the sibling `loom-daemon-start.sh` — the command the divergence
line always named — replaying **only** the allowlisted autonomy flags the last
start persisted to `<state home>/.daemon.flags` (#3968), so the FLAGS-OFF/opt-in
contract cannot widen across an unattended recovery, inside a hard
`LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS` (120) budget so a wedged start cannot wedge
the tick. **An episode ends only when a tick observes a healthy daemon** — that,
not elapsed time, is what clears the tally, the backoff clock and the escalation
sentinel, so a daemon that flaps back up gets a full fresh budget while one that
stays down does not.

Recovery can be turned off with `LOOM_WATCHDOG_AUTO_RECOVER=0`, in which case
the watchdog **says so in its own DIVERGENCE text** ("REPORT-ONLY … DETECTION,
not self-healing") — an operator must never be able to read "watchdog job
installed" as "this host self-heals". The same explicit wording fires when no
`loom-daemon-start.sh` is resolvable beside the watchdog.

The confirmed-hang path (#4398, a wedged-but-alive daemon) stays report-only and
is unchanged: the only real fix there is killing the process, which would equally
kill a daemon under heavy legitimate load.

| File | Env override | Config key | Default |
|------|--------------|-----------|---------|
| heartbeat cadence | `LOOM_DAEMON_HEARTBEAT_INTERVAL_SECS` | `autonomous.heartbeat.intervalSecs` | `60` |
| heartbeat on/off | `LOOM_DAEMON_HEARTBEAT` | `autonomous.heartbeat.enabled` | `true` (on) |
| watchdog interval | `LOOM_WATCHDOG_INTERVAL_SECS` | — | `300` (launchd `StartInterval` / systemd `OnUnitActiveSec`+`OnBootSec`) |
| watchdog job/unit basename override | `LOOM_WATCHDOG_LABEL` | — | `<daemon label/unit>-watchdog` |
| staleness threshold | `LOOM_DAEMON_HEARTBEAT_STALE_SECS` | — | `max(5 × cadence, 300)` |
| IPC probe on/off (#4398) | `LOOM_WATCHDOG_IPC_PROBE` | — | on |
| IPC probe budget (#4398) | `LOOM_WATCHDOG_STATUS_PROBE_TIMEOUT_SECS` | — | `15` |
| IPC probe argv (#4398) | `LOOM_WATCHDOG_IPC_PROBE_ARGS` | — | `quarantine list` |
| confirmed-hang threshold (#4398) | `LOOM_WATCHDOG_IPC_PROBE_FAIL_THRESHOLD` | — | `3` consecutive ticks |
| IPC probe startup grace (#4398) | `LOOM_WATCHDOG_IPC_PROBE_GRACE_SECS` | — | `LOOM_DAEMON_STARTUP_GRACE_SECS`, else `90` |
| pid file path (#5118) | `LOOM_PID_FILE` | — | the start script's `<state home>/.daemon.pid`, exported into the daemon **and** watchdog job/unit |
| auto-recovery on/off (#5391) | `LOOM_WATCHDOG_AUTO_RECOVER` | — | on |
| recovery attempt budget (#5391) | `LOOM_WATCHDOG_RECOVER_MAX_ATTEMPTS` | — | `5` per outage episode |
| recovery backoff base / cap (#5391) | `LOOM_WATCHDOG_RECOVER_BACKOFF_SECS` / `..._CAP_SECS` | — | `60` / `1800` |
| recovery command budget (#5391) | `LOOM_WATCHDOG_RECOVER_TIMEOUT_SECS` | — | `120` |
| recovery command override (#5391) | `LOOM_WATCHDOG_RECOVER_CMD` | — | sibling `loom-daemon-start.sh` + allowlisted `.daemon.flags` |
| outage escalation on/off (#5391) | `LOOM_WATCHDOG_ESCALATE` | — | on (one deduped `create-issue.sh` filing per episode) |
| provisioning-guard on/off (#5405) | `LOOM_WATCHDOG_PROVISIONING_GUARD` | `autonomous.watchdogProvisioningGuard.enabled` | `true` (on) |
| provisioning-guard cadence (#5405) | `LOOM_WATCHDOG_PROVISIONING_GUARD_INTERVAL_SECS` | `autonomous.watchdogProvisioningGuard.intervalSecs` | `600` (10 min) |

**Watchdog-provisioning guard (#5405).** The watchdog job/timer above is
provisioned by `loom-daemon-start.sh` — but only when that script is *re-run*
(an operator by hand, `fleet add-worker`, or `loom-daemon-update.sh`'s
non-launchd restart). A host provisioned before #5343 that simply keeps running
never gets healed, since #5391's recovery re-runs the start script *from the
watchdog* — which an unprotected host does not have. The daemon closes that gap
itself: a default-on periodic loop (`watchdog_provisioning_guard.rs`) re-invokes
`loom-daemon-start.sh --heal-watchdog-only` every cadence — a narrow entry point
that runs **only** `heal_watchdog_provisioning_gap()` (#5343, reused verbatim)
and never reaches the daemon-start path, so it can never disturb the running
daemon or start a second one. Cheap when idle: it does a single `autonomy-desired`
marker read per tick and only spawns the script when the marker is present.

**Why an interval timer, not a resident process or `KeepAlive`/`Restart=`.** The
reporter must itself be supervised, but a long-lived resident watchdog just moves
the who-watches-the-watchdog problem up a level (it too can crash and stay dead).
Both scheduling mechanisms own **no long-lived process**: launchd re-runs a
`StartInterval` job every interval regardless of how the last run exited, and
systemd's `.timer` re-fires its paired `Type=oneshot` service the same way — so
neither can crash-and-stay-dead. `KeepAlive`/`Restart=` are deliberately **not**
set on the watchdog job/service — that would busy-loop a short-lived job/oneshot
service instead of driving it off a fixed interval clock. The watchdog exit code
(`0` healthy / no daemon expected, `1` divergence, `3` liveness undetermined —
#5118) is for testability + a human running it by hand; neither a
`StartInterval` job's nor a timer-fired oneshot service's exit code affects the
next scheduled run.

**Decision matrix** (marker present ⇒ a daemon is expected):

| Marker | Reality | Watchdog |
|--------|---------|----------|
| present | daemon alive, heartbeat fresh, IPC round-trip OK | silent (OK) |
| present | no live pid/job found out-of-band, but the **socket answers** (#5118) | silent (OK) — the round-trip is authoritative; an absent/stale pid file is a hint, not an outage |
| present | supervisor says its job is **down** while the socket **answers** (#5118) | **report (WARN)** — an UNSUPERVISED but serving daemon; auto-remediation is suppressed (relaunching into a served socket can only be refused) |
| present | no live pid/job **and** the socket probe could not run at all (#5118) | **report (UNKNOWN, exit 3)** — liveness undetermined, explicitly *not* "the daemon is down" |
| present | daemon alive, heartbeat fresh, **IPC round-trip fails** (#4398) | **report** — 1 tick: "not yet confirmed"; N consecutive: **IPC UNRESPONSIVE (CONFIRMED)** |
| present | daemon alive, heartbeat **stale**, written this boot (younger than the process) | **report** — daemon may be wedged |
| present | daemon alive, heartbeat **older than the process itself** (#4368: a previous-boot/enablement leftover) | silent (liveness-only; not evidence about the current process) |
| present | daemon alive, no heartbeat file | silent (liveness-only; heartbeat disabled or not yet written) |
| present | daemon **not loaded/alive**, and the socket confirms it is unreachable | **report** — the #4011 outage |
| **absent** | **nothing running** | silent — deliberate stop, no false page |
| **absent** | **daemon IS running** | **report (WARN)** — state mismatch, crash protection disarmed (#4331) |

**In-band hang detection (#4398).** Rows 1–2 above are the #4398 addition. Both
pre-existing checks are *out-of-band*: neither ever talks to the daemon over the
socket it serves work on, and two incidents proved that is not enough. In #4381
the installed binary was replaced by a stub that answered `--version` and then
hung forever — a pid-alive check passes against that indefinitely. On 2026-07-29
the production daemon (pid 1484) was alive **and writing a fresh heartbeat**
while every `loom-daemon status` round-trip hung: the heartbeat writer
(`daemon_heartbeat::spawn_heartbeat_task`) and the IPC accept loop are
independent `tokio::spawn`ed tasks on a multi-threaded runtime, so one keeps
ticking while the other is wedged. After liveness is confirmed the watchdog now
also runs a **bounded in-band round-trip** through the resolved `loom-daemon`
CLI, with these properties:

- **Bounded twice.** The CLI bounds its own connect + round-trip (5s each,
  `query_daemon_bounded`); the watchdog additionally wraps the invocation in a
  hard external budget (default 15s), so even a CLI that never returns at all
  cannot hang the tick. macOS ships no `timeout(1)`, so the no-`timeout` path is
  a real built-in bounded runner, not a degrade-to-unbounded.
- **`quarantine list`, not `status --json`.** `status --json` looks like the
  obvious probe but, *after* a successful round-trip, it also runs a per-account
  token-pool network check plus a self-update check — measured at **15.3s against
  a healthy daemon**, which would make the probe budget itself the
  false-positive generator. `quarantine list` is a pure IPC round-trip with no
  post-reply work (~5ms). Override with `LOOM_WATCHDOG_IPC_PROBE_ARGS`.
- **Debounced, pid-keyed.** One failed round-trip can be transient contention
  (#4279). A single failure is reported loudly but only **N consecutive**
  failures (default 3), tallied in `<loom_dir>/.watchdog-probe-fail-count` and
  **keyed to the live pid** so a relaunched daemon never inherits its
  predecessor's streak, are called a confirmed hang. A successful probe clears
  the streak.
- **Startup-grace aware.** A probe against a process younger than the grace
  window (default 90s, the same `daemon_install_state::DEFAULT_STARTUP_GRACE_SECS`
  `status` uses) is skipped outright and never counts toward the tally — the
  post-relaunch socket-bind window is not a wedge.
- **Gracefully degrading.** No resolvable `loom-daemon` binary, a build whose CLI
  does not know the probe subcommand, or a daemon-side *application* error (which
  proves IPC works) all skip the probe rather than invent a divergence. The probe
  never becomes a new hard dependency that pages on its own absence.
- **Report-only, deliberately.** Unlike #4232's narrow auto-`kickstart`, there is
  no provably-safe unattended remediation for a wedged-but-alive process — the
  only real fix is killing it, which would equally kill a daemon merely under
  heavy legitimate load. A confirmed hang escalates to a maximally actionable
  DIVERGENCE report (distinct `IPC UNRESPONSIVE (CONFIRMED)` text, explicit
  recovery commands, exit `1`) and stops there. Auto-kill remediation, if ever
  wanted, needs its own narrow provably-safe gate.

The last no-marker row is the #4331 fix. Before it, a missing marker short-circuited to a
bare `[OK] … nothing to check` **without probing reality at all** — so a
supervised daemon running with its marker gone (see "Marker ownership" below) was
reported healthy while the watchdog would in fact never revive it. The no-marker
branch now runs the *same* liveness probe as the marker-present path (env-derived
defaults: `LOOM_LAUNCHD_LABEL` or the default label; `<loom_dir>/.daemon.pid` on
the non-launchd path) and, only when a daemon **is** demonstrably alive, WARNs +
exits `1`. The load-bearing quiet case — marker absent *and* nothing alive, i.e. a
deliberate `loom-daemon-stop.sh` (which also boots the daemon job out, so nothing
is found) — stays exactly as silent as before.

**Socket-first liveness, pid file demoted to a hint (#5118).** Until #5118 the
out-of-band probe was the *only* thing that could establish liveness, and on a
host with no launchd job and no systemd-managed unit to ask, that reduced to one
artifact: the pid file. Three defects stacked on it. (a) **Path disagreement** —
the watchdog derived the path from the *socket's* directory while the daemon
derives it from `LOOM_PID_FILE` / the workspace (`daemon_pidfile.rs`); on a
workspace-rooted install those are never the same directory. (b) **`LOOM_PID_FILE`
was ignored by the watchdog** even though `loom-daemon-start.sh` exports it and
the daemon honors it as tier 1. (c) **A stale pid file read as a confirmed
death.** Observed 2026-08-03: *both* fleet hosts ran healthy daemons while the
watchdog reported `[DIVERGENCE] … no live pid file at ~/.loom/.daemon.pid` every
five minutes for two days — and two genuine outages in that window were
indistinguishable from the steady-state false alarm.

The precedence is now the same one `loom-daemon health` already uses: **the IPC
round-trip is authoritative, the pid file corroborates**. When the out-of-band
probe finds no live daemon the watchdog *asks the socket* before reporting
anything (see the three new decision-matrix rows above), and the pid file's path
is resolved by the identical precedence the daemon uses — `LOOM_PID_FILE` > the
marker's `pid_file=` > `LOOM_MACHINE_CHECKOUT` (machine-level loom dir) >
`LOOM_WORKSPACE`/`repo_root` (`<repo>/.loom`) > `<loom_dir>`. `LOOM_PID_FILE` is
now also baked into the **watchdog** job/unit (previously only the daemon's), so
both ends read the file the start script chose. The `UNKNOWN`/exit-3 state is the
other half of the fix: an alerting component that cannot determine liveness must
say so, because defaulting to "the daemon is gone" is precisely what turned this
detector into a permanent false positive.

**Marker lifetime across a self-update.** `loom-daemon-update.sh` performs an
internal stop→start, which is a **restart**, not operator intent to stop — so it
passes `loom-daemon-stop.sh --restarting` (equivalently
`LOOM_DAEMON_STOP_KEEP_INTENT=1`), which **preserves** the marker + watchdog. This
is load-bearing: inferring restart-vs-stop would mean every self-update silently
disarms the detector — the exact bug class #4011 fixes — so it is an explicit
signal, never an inference. The subsequent start re-writes the marker and
re-provisions the watchdog.

**Marker ownership (create / preserve / remove per surface).** The marker's
lifetime is operator intent, spread across several surfaces — this table is the
contract the healing + detection below implement:

| Surface | Marker behavior | Reference |
|---------|-----------------|-----------|
| `loom-daemon-start.sh` | **Creates** it (`write_intent_marker`) on every successful start (launchd, systemd, nohup) | `loom-daemon-start.sh` (`write_intent_marker`) |
| `loom-daemon-stop.sh` | **Removes** it + tears down the watchdog — unless `--restarting` / `LOOM_DAEMON_STOP_KEEP_INTENT=1`, which **preserves** both | `loom-daemon-stop.sh` |
| `loom-daemon-update.sh` (full roll) | **Preserves + re-creates**: `stop --restarting` (preserve) then `start.sh` (re-write) | `loom-daemon-update.sh` |
| `loom-daemon restart` (#4054 primitive) | **Preserved if present** (the daemon exits `EXIT_RESTART`; the supervisor relaunches, marker untouched). An **absent** marker is **healed at startup** — see below | `ipc.rs`, `autonomy_marker.rs` |
| in-daemon self-update loop | Uses `update.sh --no-restart` + the restart primitive — bypasses the stop/start rewrite; relies on **startup healing** | `main.rs`, `autonomy_marker.rs` |
| bare launchd `KeepAlive` / systemd `Restart=on-success` relaunch | Never runs the start script — relies on **startup healing** | `autonomy_marker.rs` |

**Startup marker healing (#4331).** The restart primitive, the self-update loop,
and a bare supervisor relaunch all bring up a fresh daemon **without** re-running
`write_intent_marker`, so before #4331 an *absent* marker was never re-created
while a supervised daemon kept running — the daemon ran with crash protection
silently disarmed, forever. Rather than patch each restart path, the daemon heals
the marker at **one startup choke point** (`autonomy_marker::heal_on_startup`,
wired in `main.rs` right after the heartbeat loop): if it detects it is supervised
([`ipc::detect_supervisor`] ⇒ `Some`, from the `LOOM_DAEMON_SUPERVISOR` env the
plist/unit bake in) **and** the marker is absent, it re-writes it. That single
point covers the primitive, the self-update loop, and a bare relaunch. Deliberate
constraints:

- **Never for an unsupervised run.** A `--foreground` / nohup / debug start
  (`detect_supervisor` ⇒ `None`) writes **no** marker — otherwise every dev run
  would arm the host-side pager after the dev session exits.
- **`LOOM_AUTONOMY_MARKER` respected.** The path is resolved with the same env
  override the plist/unit export, so healing and the watchdog agree.
- **Never overwrites a present marker** (it already encodes start-time intent);
  and a failed write is logged, never fatal. The healed marker mirrors
  `write_intent_marker`'s fields byte-for-byte (with a `# HEALED …` provenance
  comment) so the watchdog and `daemon_install_state` parse it identically.

**Platform + isolation.** Darwin and systemd Linux hosts both get a provisioned,
scheduled watchdog (see the two bullets above). Only the **nohup fallback tier**
— a non-systemd Linux host, or an explicit `--no-launchd`/`--no-systemd` /
`LOOM_DAEMON_LAUNCHD=0`/`LOOM_DAEMON_SYSTEMD=0` escape hatch — has no host-side
checker provisioned: the marker + heartbeat are still written, and
`loom-daemon-watchdog.sh` can be run by hand or wired to cron. `<loom_dir>` is the
parent of `LOOM_SOCKET_PATH` (else `~/.loom`), so pointing `LOOM_SOCKET_PATH` at a
tempdir isolates the marker + heartbeat there too — which is how the lifecycle
tests avoid ever touching the operator's real `~/.loom`. A forge-side reporting
channel is an explicit follow-up (out of scope for #4011).

**Third consumer: `loom-daemon status` (#4069).** The watchdog's marker +
heartbeat inputs have a second, on-demand consumer: `status`'s
unreachable-daemon error path (`main.rs`'s `handle_status_command`, via the
library module `daemon_install_state.rs`). Before #4069 every transport
failure collapsed into one generic *"Could not reach loom-daemon… Is the
daemon running?"* message, regardless of whether autonomy was ever expected.
`status` now runs the **same read-only, local probe** — same marker, same
per-field fallbacks, same env-wins-over-marker rule, same heartbeat staleness
formula — so `status` and `<loom_dir>/logs/daemon-watchdog.log` can never
disagree, and reports one of four states with a distinct exit code:

| State | Meaning | Exit code | Remediation printed |
|-------|---------|-----------|----------------------|
| `not-expected` | marker absent (deliberate stop, or never started) | `1` | suggest `loom-daemon-start.sh` |
| `expected-but-dead` | marker present, no live process — the #4011 divergence | `3` | suggest `loom-daemon-start.sh`; points at `daemon-watchdog.log` |
| `alive-starting` | marker present, process alive, IPC failed, **process age ≤ startup-grace window** — a normal `bootout`/`bootstrap` restart whose socket has not bound yet (#4213) | `4` | **none** — reports "still starting, not a fault"; NO stop/start remediation |
| `alive-but-unresponsive` | marker present, process alive, IPC failed, process **older** than the grace window (or age undeterminable) | `4` | does **not** suggest a start (singleton guard refuses); prints the live pid; heartbeat freshness qualifies fresh ⇒ IPC/socket fault, stale ⇒ likely wedged, **prior-boot** ⇒ not evidence about the current process |

The heartbeat freshness qualifier on `alive-but-unresponsive` also gates the
"do NOT run `loom-daemon-start.sh` … stop && start" remediation (#4368): it is
only printed for a **current-boot `stale`** verdict — heartbeat age exceeds
the staleness threshold *and* is younger than the process's own age, i.e. the
evidence actually points at a wedge. `fresh` / `unknown` / **`prior-boot`**
print inspect-first guidance (`ps -p <pid> -o pid,etime,command`, `loom-daemon
status --json`) instead of the imperative restart, since none of those three
qualifiers indicate a fault. `prior-boot` fires when the heartbeat file's mtime
is strictly older than the live process's own start time (`heartbeat_age_secs
> process_age_secs`) — necessarily a leftover from a previous boot or a
previous enablement of the opt-in heartbeat loop, checked *before* the
staleness threshold (even a heartbeat that would otherwise look "fresh" by age
alone is not current-boot evidence if it predates the process). Equal ages are
deliberately **not** `prior-boot` (current-boot evidence, falls through to the
ordinary threshold check); an undeterminable process age makes **no**
prior-boot claim and degrades to the pre-#4368 fresh/stale verdicts.
`loom-daemon-watchdog.sh` §3 mirrors the same mtime-vs-process-start
comparison (via `ps -o etime= -p <pid>`) before its own stale WARN, so `status`
and the watchdog log can never contradict each other on this qualifier either.

The startup-grace window defaults to `90s` (sized above the observed ~40–60s
socket-bind latency after a `launchctl bootout`/`bootstrap` cycle) and is
overridable via `LOOM_DAEMON_STARTUP_GRACE_SECS` (env > default, mirroring the
`LOOM_DAEMON_HEARTBEAT_STALE_SECS` precedence). Process age is read from
`ps -o etime= -p <pid>`; an unparseable age makes **no** grace claim and falls
through to `alive-but-unresponsive`, never a false "starting". The grace
discriminator is process age **alone** — never socket-file presence, since a
stale socket file from the prior run can legitimately still exist during
startup. `loom-daemon-watchdog.sh` never probes IPC, so it cannot emit the
fault verdict and needs no matching grace state.

**Correlating the two views by timestamp (#5790).** `status` and the watchdog
log are the operator's only two views of the same fault, and until #5790 they
shared no anchor: `status` reported ages *relative to the moment it ran*
("heartbeat is fresh (46s ago)"), while every `daemon-watchdog.log` line is
absolute UTC. A captured `status` invocation therefore could not be lined up
against the log after the fact, and the pair read as contradictory evidence —
the exact complaint in the #5790 incident report (`status` timing out on IPC
twice ~20 minutes apart while the watchdog log showed only `[OK]`). The
unreachable path now prints an absolute `Observed at <UTC>` line in the
watchdog's own `%Y-%m-%dT%H:%M:%SZ` format immediately under the "Could not
reach…" line, restates each relative age with its absolute `last write <UTC>`
instant, and — on `alive-but-unresponsive`, the incident's state — prints the
`daemon-watchdog.log` path with an explicit instruction to correlate that
window. `--json` carries the same anchors as additive keys: top-level
`observed_at` and `install_state.heartbeat.last_write_at`. Reconciling the two
sources is now a literal `grep` of the stamp, not clock arithmetic.

`--json` gains a structured `install_state` object (`state` is the
machine-readable enum above, plus `started_at`, `pid`, `liveness_detail`, a
`heartbeat` sub-object — `freshness`, `age_secs`, `last_write_at`,
`stale_threshold_secs` —, `process_age_secs`, `startup_grace_threshold_secs`,
and `watchdog_log`), alongside the top-level `observed_at` stamp described
above; the pre-#4069 `error` string key is retained for
compatibility. The probe never fails the command — an
unreadable/malformed marker, absent `launchctl`, a stale/unowned pid, or an
unreadable heartbeat mtime all degrade to a less-specific verdict (or, if no
loom dir can be resolved at all, `install_state` is simply omitted and the
generic pre-#4069 message is printed). `install_state` itself only ever runs
after `query_daemon_status` has already failed — the reachable path's own
protection reporting is the separate probe described next.

**Fourth consumer: `status`'s REACHABLE path — watchdog protection state
(#4354, AC4 of #4331).** `install_state` above answers *"why is the daemon
unreachable?"*, so it runs only on the `Err` arm. A **reachable** daemon (one
answering over IPC) needs a different question answered: *"is it actually
protected?"* Before #4354 it did not surface one, so a healthy-looking daemon
could be running with crash protection silently disarmed, and an operator would
only find out by reading `daemon-watchdog.log` or poking
`launchctl`/`systemctl` by hand. `status` now prints a `Protection:` line (and a
`protection` object under `--json`) built from a **sibling** classification —
`ProtectionState` in the same `daemon_install_state.rs` module, deliberately
*not* new `InstallState` variants, whose exit-code semantics above must not
change:

| State (`--json` `state`) | Human phrase | Meaning |
|--------------------------|--------------|---------|
| `protected` | `protected` | autonomy-desired marker present **and** the watchdog job/timer is provisioned — i.e. a confirmed death is detected AND (since #5391, unless `LOOM_WATCHDOG_AUTO_RECOVER=0`) auto-recovered under a bounded-retry circuit breaker, escalating to a forge issue if the budget is spent |
| `no-marker` | `unprotected — no autonomy-desired marker` | no marker: crash protection is DISARMED — the watchdog fires on cadence but logs `[OK] … nothing to check` (the #4331 state) |
| `watchdog-not-provisioned` | `watchdog job not provisioned` | marker present, but no watchdog launchd job / systemd timer is scheduled — nothing will ever notice a future death |
| `unknown` | `unknown` | marker present but the provisioning probe could not answer (no `launchctl`/`systemctl`, or an unreachable `systemctl --user` bus) — a degradation, never a false verdict |

Two independent facts feed this, and **both** are always reported: the JSON
object carries `marker_present` and `watchdog_provisioned` separately (the
latter `null` when undeterminable) alongside `marker_path`, `watchdog_job`,
`watchdog_job_kind`, `detail`, and the dominant `state`. When both are bad,
`state` is `no-marker` — the stronger statement, since even a provisioned
watchdog is inert without a marker — while `watchdog_provisioned: false` still
records the second fact for a script.

**How the probe resolves its inputs** (client-side and host-local, so nothing is
plumbed through the IPC report):

- **Marker** — via the same `LOOM_AUTONOMY_MARKER`-honoring resolver the startup
  healer uses (`autonomy_marker::resolve_marker_path`, now the single
  implementation `daemon_install_state` also delegates to), else
  `<loom_dir>/autonomy-desired`.
- **Watchdog job** — mirrors `loom-daemon-start.sh` exactly: launchd
  `${LOOM_WATCHDOG_LABEL:-<daemon-label>-watchdog}` (daemon label from
  `LOOM_LAUNCHD_LABEL`, else the marker's own `launchd_label`, else
  `com.rjwalters.loom-daemon`), probed with
  `launchctl print <domain>/<label>` in the `resolve_launchd_domain` domain
  (`LOOM_LAUNCHD_DOMAIN` → `gui/<uid>` → `user/<uid>`); systemd
  `${LOOM_WATCHDOG_LABEL:-<LOOM_SYSTEMD_UNIT%.service>-watchdog}.timer`, probed
  with `systemctl --user is-enabled <unit>`. A **non-Darwin host always resolves
  the systemd side** (the same non-Darwin blanket override the unreachable path
  applies), so `launchctl`'s absence on Linux is never even consulted.
- **Provisioning verdict** — `enabled`/`enabled-runtime` ⇒ provisioned; any other
  `is-enabled` verdict (`disabled`, `static`, `masked`, absent, …) ⇒ **not**
  provisioned, deliberately collapsing "present but disabled" and "absent" into
  the same operator-relevant answer (neither will fire). A `Failed to connect to
  bus` / missing-binary / undeterminable-domain outcome ⇒ `unknown`.

Like the `install_state` probe, it is **read-only and never fails the command**:
at most three query-only subprocess calls, no writes, and the reachable path
still **exits 0** whatever protection state it reports (only `no-marker` and
`watchdog-not-provisioned` add remediation lines — `protected` and `unknown`
print none, since `unknown` is a probe limitation on this host, not evidence of a
fault). When no loom dir resolves at all the `Protection:` line is omitted and
`--json`'s `protection` is `null`. The unreachable-path `install_state` output
and its exit codes are unchanged. `fleet status`'s local-host row carries the
same `protection` field, since it shares the `status --json` payload builder.

**Marker-vs-non-autonomous-daemon mismatch (#4693).** `ProtectionState` above
answers "would we notice a future DEATH?" — but a daemon can be perfectly
`protected` by that definition (marker present, watchdog provisioned) while
having **silently stopped dispatching**, because a plain `loom-daemon-start.sh`
re-render is FLAGS-OFF by default (#3911) even on a host that was previously
autonomous. This was the exact 2026-07-30 incident: `status` reported a
healthy, protected daemon while 23 ready issues sat queued for ~3h. The `--json`
`protection` object therefore also carries `autonomy_mismatch: bool` — `true`
only when the marker is present **and** this reachable daemon's own
`work_finder` (a sibling top-level object, `{"enabled": bool|null}`, resolved
daemon-side from `work_finder::resolve_enabled` — the daemon's own env/config,
never the CLI client's) reads `false`. `null`/absent `work_finder.enabled`
(a pre-#4693 daemon binary) degrades to `autonomy_mismatch: false` — never a
false positive. The human `Protection:` block prints a loud `WARNING:` with the
`--work-finder` / `--from-config` remediation when it fires.

`loom-daemon-start.sh` closes the writing side of the same gap: before
re-rendering the plist/unit, it compares the new render's `LOOM_WORK_FINDER` /
`LOOM_MAIN_HEALTH_GATE` against the **prior installed** plist/unit's value (or,
absent a readable prior value, the autonomy-desired marker alone) and prints a
loud, non-blocking `WARNING: autonomy downgrade — <key>: 1 -> 0` when a plain
(no-flags, non-`--from-config`) start would downgrade a previously-autonomous
host — naming the `--from-config` / `--work-finder` remediation. This never
reverses the FLAGS-OFF-by-default policy for a start with no prior-autonomy
signal at all (#3911) — it only closes the *silent* part of an actual
autonomous → FLAGS-OFF transition.

### macOS session-bootstrap hazard (#3972)

**Incident (2026-07-26).** The daemon was started at 21:48 via
`loom-daemon-start.sh` from inside a Claude Code session. That session crashed
at 02:49. From the very next work-finder tick (02:50:21), **every** `gh` call
in the daemon's process tree failed with `tls: failed to verify certificate:
x509: OSStatus -26276` and `git fetch` failed with `No user exists for uid 501`
— while the identical commands worked perfectly from any fresh shell. The
daemon ran blind for ~35 minutes: the work finder saw 0 issues (4 errors/tick),
the main-health gate went RED on purely environmental failures, and in-flight
sweep children couldn't reach the forge either.

**Root cause.** The pre-#3972 `loom-daemon-start.sh` detached the process with
a plain `nohup "$DAEMON_BIN" … &`. `nohup` makes the process immune to
`SIGHUP`, but it does **not** move the process out of the *launching session's*
Mach bootstrap namespace — the process stays registered under whichever
terminal/Claude-Code-session/SSH-connection Mach service happened to spawn it.
When that session's context is torn down, XPC lookups the daemon (and every
child it spawns) depend on start failing with no crash and no obvious log
signal:

- **`trustd`** (certificate verification) — the underlying cause of the `gh`
  TLS/OSStatus errors, since Go's Darwin certificate verifier round-trips
  through `trustd` via XPC.
- **`opendirectoryd`** (`getpwuid`) — the underlying cause of git's
  "No user exists for uid N", since `git` resolves the current user via
  `getpwuid()`, which is backed by `opendirectoryd` on macOS.

This is why **"start it from a terminal that might die" is unsafe on macOS**.
Linux does not have this failure mode — a systemd user session (or a plain
`nohup` under `init`) does not tie a background process's XPC-equivalent
identity to the shell that spawned it.

**Fix.** `loom-daemon-start.sh` now generates a `launchd` LaunchAgent plist and
loads it with `launchctl bootstrap <domain>` (the resolved per-user domain — see
"launchd domain resolution" below) instead of `nohup`-backgrounding in-process
(Darwin only — Linux is unaffected and keeps the plain nohup path). This was
validated during the incident itself: relaunching the identical binary as a
launchd agent immediately restored `gh`/`git` — the first tick after migration
reported `13 seen, 3 dispatched, 0 error(s)`.

**launchd domain resolution (#4130) — gui/<uid> ↦ user/<uid>.** The daemon was
originally loaded into the hardcoded `gui/<uid>` domain — the per-GUI-login
(Aqua) domain owned by `loginwindow`. Over SSH with **no active GUI login
session** that domain does not exist, so `launchctl bootstrap gui/<uid> …` fails
with `error 125: Domain does not support specified action`, and the daemon could
not be (re)started remotely on a headless Mac. The shared resolver
`resolve_launchd_domain()` (`defaults/scripts/lib/launchd-domain.sh`, consumed by
`loom-daemon-start.sh` / `-stop.sh` / `-update.sh` / `-watchdog.sh` so the whole
lifecycle agrees on one domain) resolves in this order:

1. an explicit `LOOM_LAUNCHD_DOMAIN` override, honored **verbatim** (a pinned
   domain that does not resolve fails loudly at `bootstrap`, never falls back
   further — the override is honored, not advisory);
2. `gui/<uid>` when `launchctl print gui/<uid>` resolves (a live GUI login) —
   **byte-for-byte the pre-#4130 default** on a host with an Aqua session;
3. `user/<uid>` — the **background per-user** domain that `sshd` itself
   instantiates: present over SSH with no GUI, running as the *user* (not root),
   and `gui/<uid>` already forwards to it (`endpoint destination =
   …domain.user.<uid>`).

**Rejected alternatives** (documented, deliberately not implemented):

| Mechanism | Runs as | Needs GUI session? | Needs sudo? | Why rejected |
|---|---|---|---|---|
| `gui/<uid>` (pre-#4130 default) | user | **yes** | no | Fails over SSH — the defect this fixes. Kept as the preferred domain **when it resolves**. |
| **`user/<uid>` (chosen fallback)** | user | no | no (own uid) | Background session ⇒ TCC prompts can't be answered interactively (see #3980) and the login keychain may be locked (see #4005). Smallest change, closest privilege match. |
| `launchctl asuser <uid>` | user | effectively yes (targets a session) | yes | Still needs a target session to impersonate. |
| system `LaunchDaemon` | **root** by default | no | yes | Wrong privilege model and **no login-keychain session** — re-opens the #4005 headless-credential problem in a new place instead of closing one. |

**Consequences of a non-Aqua (`user/<uid>`) domain.** A background domain cannot
surface an interactive TCC prompt, so any touch of a protected folder by the
daemon or a sweep child fails silently rather than prompting — see
`### macOS TCC hygiene under launchd (#3980)` below (the daemon's legitimate
working set never needs a protected folder, so this is a diagnostic signal, not a
blocker). The operator's **login keychain** may also be locked in a headless
session, so export a `GH_TOKEN` for forge auth rather than relying on the keychain
(the #4005 credential preflight reports this loudly). Both are inherent to running
without an Aqua session, not regressions introduced here.

- **Plist location & label**: `~/Library/LaunchAgents/com.rjwalters.loom-daemon.plist`
  (override the label with `LOOM_LAUNCHD_LABEL`; override the domain with
  `LOOM_LAUNCHD_DOMAIN`). Regenerated and reloaded (`bootout` the old definition,
  then a fresh `bootstrap` + `kickstart -k`) on **every** start, so a later
  invocation's flags/env always win over a stale loaded definition.
- **Environment forwarding**: the plist's `PATH` is the current `PATH` plus a
  fallback set (`~/.local/bin`, `~/.cargo/bin`, Homebrew, standard bin dirs) so
  `gh`, `git`, `cargo`, and `python3` resolve even inside launchd's minimal
  environment. Every already-exported `LOOM_*` / `GH_TOKEN` / `GITEA_TOKEN` /
  `FORGE_TOKEN` var is forwarded verbatim, so the FLAGS-OFF / `--work-finder` /
  `--health-gate` / `--from-config` semantics are preserved exactly — the
  launchd job never sees a wider or narrower autonomy configuration than a
  plain nohup start would have resolved.
- **Token hardening + startup credential preflight (#4005)**: because a
  forwarded `GH_TOKEN`/`GITEA_TOKEN`/`FORGE_TOKEN` is embedded verbatim in the
  plist, `loom-daemon-start.sh` writes the file mode `0600` whenever it
  carries one of those vars (otherwise it inherits the process umask —
  typically world-readable `0644`). Separately, the daemon resolves its own
  forge credential once at boot — before its first `gh` consumer — and logs
  the outcome loudly (`info!`/`error!`, never the token value); the result is
  also surfaced in `loom-daemon status` (`credential_preflight` in `--json`,
  a "Forge credential: OK/DEGRADED" line in the human view). See
  [`github-authentication.md` § Headless and SSH-only daemon operation](github-authentication.md#headless-and-ssh-only-daemon-operation-4005)
  for the operator-facing walkthrough — this is the fix for the
  keychain-unlock-dependency failure mode: a daemon started over SSH with no
  exported token used to boot clean and then 401 silently on every forge call.
- **`RunAtLoad=true` / `KeepAlive={SuccessfulExit:true}`** (#4054): `RunAtLoad=true`
  means the daemon survives a reboot/re-login, not just the death of one particular
  session — strictly *more* durable than the pre-#3972 nohup contract (which didn't
  survive a reboot either). `KeepAlive={SuccessfulExit:true}` (changed from the
  earlier bare `KeepAlive=false`) is the **supervised restart primitive**: launchd
  relaunches the job **only** when it exits with status `0`, and leaves it down on
  any non-zero exit. Because the daemon exits **non-zero** on a crash/panic, on a
  SIGTERM operator stop (143), and on a SIGINT/Ctrl-C (130), this **preserves** the
  old no-crash-loop semantics of `KeepAlive=false` — none of those respawn — while
  making the one deliberate clean-exit path (the `RestartDaemon` request, `loom-daemon
  restart`) the *only* thing that trips a relaunch. `loom-daemon-stop.sh` still
  `bootout`s the loaded definition after confirming the process is dead (so an
  explicit stop is honored at the next login too), but the bootout is now
  belt-and-braces: the non-zero SIGTERM exit already prevents launchd from relaunching
  during the stop window. See [Supervised restart primitive](#supervised-restart-primitive-4054)
  below and `docs/design/supervised-restart-primitive.md`.
- **Escape hatch**: `--no-launchd` (or `LOOM_DAEMON_LAUNCHD=0`) forces the
  legacy nohup path even on Darwin — e.g. for a sandboxed macOS runner where no
  launchd domain is usable at all. Note that a headless/SSH-only session **no
  longer requires** this escape hatch just to start: `resolve_launchd_domain()`
  falls back to `user/<uid>` (above), so the durable launchd path (and its #3972
  Mach-bootstrap-namespace protection) is available over SSH too — nohup remains
  only the last-resort opt-out, not the headless default.
- **Inspection without side effects**: `--print-plist` renders the exact plist
  XML this invocation would install and exits — no `launchctl` call, no file
  write to `~/Library/LaunchAgents`. Useful for auditing exactly what
  environment/flags a given invocation would forward.
- **Linux**: on a systemd host the daemon is supervised as a `systemd --user`
  service (#4268), not a bare `nohup` — see "systemd user unit (Linux)" directly
  below. On a non-systemd host (or with `--no-systemd` / `LOOM_DAEMON_SYSTEMD=0`)
  `nohup` remains the mechanism, which is safe on Linux because a background
  process's identity is not tied to the spawning shell the way macOS's Mach
  bootstrap is (so the #3972 failure mode does not reproduce there — the systemd
  path is about reboot survival + supervised restart, not that incident).

### systemd user unit (Linux, #4268)

On a systemd Linux host, `loom-daemon-start.sh` installs a `systemd --user`
service and `systemctl --user enable --now`s it, the Linux mirror of the launchd
LaunchAgent path above (sub-issue B of #4260). This replaces the pre-#4268 bare
`nohup … &`, which had no reboot survival, no supervised restart, and no
disable-on-stop. The contract mirrors launchd point-for-point:

| launchd (Darwin) | systemd `--user` (Linux) |
|---|---|
| `RunAtLoad=true` + `launchctl enable` (#3972) | `[Install] WantedBy=default.target` + `systemctl --user enable` |
| `KeepAlive:{SuccessfulExit:true}` — relaunch only on a clean exit `0` (#4054) | `Restart=on-success` — relaunch only on a clean exit `0` (exact analog; a crash / operator SIGTERM/SIGINT exits non-zero and stays down) |
| (no cgroup-timeout reclassification of a clean exit — not applicable) | `KillMode=mixed` (#4862) — without it, a clean exit(0) with lingering cgroup children (in-flight `claude`/`tee`/`sleep` sweep workers) gets reclassified `Result=timeout` by the default `control-group` mode once `TimeoutStopSec` elapses, and `Restart=on-success` does not match `timeout` — so the relaunch silently never fires. `mixed` escalates the leftover-process SIGKILL sweep on the *main process's own exit*, not after `TimeoutStopSec`, so the unit's `Result` tracks the main process's exit status |
| (no `TimeoutStopSec` analog — launchd's `KeepAlive` has no stop-timeout escalation) | `TimeoutStopSec=20` (#4950) — the daemon's own stop paths (`RestartDaemon`'s `exit(0)`, the SIGTERM handler's `exit(143)`) are near-instant with no blocking drain, so 20s is a generous multiple, not a tight fit; it exists purely as a fast-failure backstop against a regression or a stale, not-yet-re-rendered unit, well below systemd's 90s default |
| `launchctl bootout` on operator stop | `systemctl --user disable --now <unit>` |
| plist `EnvironmentVariables` (`LOOM_DAEMON_SUPERVISOR=launchd`, forwarded `LOOM_*`/tokens, deterministic PATH #4172) | `Environment=` lines (`LOOM_DAEMON_SUPERVISOR=systemd`, same forwarded env + PATH) |
| `WorkingDirectory` = checkout in machine mode (#4229), else repo root | `WorkingDirectory=` — same resolution |
| `--no-launchd` / `LOOM_DAEMON_LAUNCHD=0` escape hatch (#4078) | `--no-systemd` / `LOOM_DAEMON_SYSTEMD=0` escape hatch |
| `--print-plist` inspection (no side effects) | `--print-unit` inspection (no side effects) |

- **Unit location & name**: `~/.config/systemd/user/loom-daemon.service` (override
  the name with `LOOM_SYSTEMD_UNIT`). Regenerated and `daemon-reload`ed on every
  start, so a later invocation's flags/env always win over a stale unit. Written
  `0600` when it carries a forwarded `GH_TOKEN`/`GITEA_TOKEN`/`FORGE_TOKEN`.
- **`LOOM_DAEMON_SUPERVISOR=systemd`** is baked into the unit so the daemon can
  prove it is supervised before it exits for a supervised restart — recognized
  daemon-side by `detect_supervisor()` (PR #4298 / #4267). It is hardcoded into the
  rendered unit (never harvested from the caller's env), so it is present on every
  supervised start and absent from the nohup path.
- **Reboot survival requires lingering.** A `systemd --user` manager only runs
  while the user has a session; the service comes back after a reboot (or an SSH
  logout) **only** when the user has lingering enabled. Run **`loginctl
  enable-linger "$USER"`** once on a headless / SSH-only host. Without it the unit
  is still installed + supervised for the life of the login session, but does not
  survive a reboot; `loom-daemon-start.sh` prints this reminder on every systemd
  start.
- **User-manager reachability fallback.** If `systemctl` is present but the
  per-user manager is unreachable — a bare SSH login with no lingering and no
  active user session, so `XDG_RUNTIME_DIR` is unset / `systemctl --user
  is-system-running` reports `offline` — the start script warns clearly (with the
  `enable-linger` remedy) and falls back to the nohup path, rather than failing
  with a cryptic `Failed to connect to bus` error. Detection lives in
  `is_linux_systemd()` / `systemd_user_manager_reachable()`
  (`defaults/scripts/lib/systemd-user.sh`, the Linux counterpart to
  `lib/launchd-domain.sh`, shared by start + stop).
- **Stop disables the unit.** `loom-daemon-stop.sh` detects the `systemd --user`
  ownership and runs `systemctl --user disable --now`, then re-verifies the unit is
  inactive and exits non-zero if it is not (the systemd analog of the launchd
  bootout-did-not-stick check). `LOOM_DAEMON_SYSTEMD=0` disables all systemd
  interaction symmetrically, so a `--no-systemd` (nohup) start gets a stop that
  never touches the user manager.
- **Crash relaunch is out of scope.** `Restart=on-success` deliberately does
  **not** relaunch a crashed daemon (a non-zero exit) — that is watchdog territory.
  On a systemd host it is delivered by the `<unit>-watchdog.timer` +
  `Type=oneshot` `.service` pair (#4260 sub-issue D, "Autonomy-loss watchdog +
  heartbeat" above), mirroring the macOS `StartInterval` autonomy-loss watchdog
  (#4011) — the watchdog mostly *reports* divergence, except for the narrow
  #4232/#4862 auto-remediation gates (unit LOADED + not running + a clean
  `ExecMainStatus=0`, mirroring `launchctl kickstart`), which run `systemctl
  --user reset-failed <unit> && systemctl --user start <unit>` — never for a
  crash or an operator stop.
- **The `restart` primitive itself also verifies, synchronously (#4950).** The
  watchdog gate above is a periodic (timer-driven) safety net; it can be
  minutes away from the moment a roll actually breaks the relaunch. So
  `loom-daemon-update.sh`'s systemd branch (mirroring #4232's launchd
  verification) polls for a NEW, live `MainPID` within a bounded window
  (`LOOM_DAEMON_RESTART_POLL_SECS`, default 30s) right after the `restart` IPC
  ack, *before* reporting success — the 2026-08-02 incident this closes was a
  `restart` that ack'd cleanly (exit 0) but whose unit landed in `failed
  (Result: timeout)` because `TimeoutStopSec` (now bounded to 20s, see
  "systemd unit rendering" below) had not yet been applied to the live,
  already-installed unit. When the poll times out AND the unit's `ActiveState`
  is confirmed `failed` — `Restart=on-success` never auto-relaunches a `failed`
  unit — it self-heals with the identical `systemctl --user reset-failed
  <unit> && systemctl --user start <unit>` recovery, re-polls
  (`LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS`, default 15s), and only then gives
  up (exit 7) with a `systemctl --user status` diagnostic snapshot.

### macOS TCC hygiene under launchd (#3980)

**Why launchd changed the TCC picture.** Under the pre-#3972 nohup model, the
daemon inherited whatever TCC (Transparency, Consent, and Control) grants the
launching terminal app already had — so folder-access prompts, if any, belonged
to Terminal.app/iTerm/Claude Code, not to `loom-daemon`. As a launchd LaunchAgent
(see above — `gui/<uid>` under a GUI login, else `user/<uid>` over SSH, #4130),
the daemon is its **own** TCC-responsible process:
any touch of a protected location (`~/Desktop`, `~/Documents`, `~/Downloads`,
`~/Pictures` (Photos), `~/Music` (Media & Apple Music),
`~/Library/Mobile Documents` (iCloud Drive), network/removable volumes, …) by
the daemon **or any sweep child it spawns** now prompts fresh, once per
protected category. One operator report saw ~10 prompts in a single session,
including Photos / Media & Apple Music / iCloud — evidence of something
enumerating the top level of `$HOME` itself rather than touching those folders
individually (macOS bundles the per-category checks into one burst when a
process lists `$HOME`'s immediate contents).

**Under the `user/<uid>` (headless/SSH) domain (#4130) there is no prompt at
all.** A background domain cannot surface an interactive TCC dialog, so the same
out-of-bounds access **fails silently** (permission-denied) instead of prompting.
That does not change the contract below — the daemon's and sweep children's
legitimate working set still contains no protected folder — it only changes the
symptom (a silent file-not-found rather than a popup). The remedy is identical:
fix the offending script/tool to stay within the working-set contract, never a
broad grant. This is one of the documented consequences of the non-Aqua fallback
domain (see "launchd domain resolution" under #3972 above).

**The daemon's legitimate working set never needs a protected folder.** Audited
surfaces — the daemon core (Rust) and `.loom/scripts/*` — only ever touch
`~/GitHub/*` (or wherever a workspace lives), `~/.loom`, `~/.claude*`, and
`/private/tmp`; disk-headroom checks use `df -Pk <workspace>`, not a directory
walk. `defaults/scripts/claude-wrapper.sh`'s crash-recovery path
(`recover_cwd()`, used when a worktree is deleted out from under a running
sweep child, e.g. by `loom-clean` or `merge-pr.sh`) previously fell back to
`cd "$HOME"` as a last resort before `/tmp` — landing a respawned `claude`
child in `$HOME` risked exactly the kind of out-of-bounds enumeration described
above. Fixed in #3980: both the last-resort `cd` and the script's initial
`WORKSPACE` fallback (when `pwd` fails at wrapper startup) now go straight to
`/tmp`, which is on the TCC-safe allowlist and serves the same "always exists,
always cd-able" purpose. `$HOME` is no longer a reachable recovery target
anywhere in the wrapper.

**Sweep children's working-set contract.** Every `/loom:sweep`-dispatched
child (Curator/Builder/Judge/Doctor/Champion subagents, and any test suite or
tool subprocess they invoke) is expected to stay within: the workspace root
it was dispatched into, `.loom/` (worktrees, logs, tokens, checkpoints),
`.claude*` config dirs, and `$TMPDIR`/`/private/tmp` scratch space. Recursive
scans that escape this contract — `find ~`, `du -sh ~`, `grep -r` rooted at
`$HOME`, a script that `cd`'d to the wrong place before globbing, a test suite
that writes fixtures to `~/Documents` instead of a tmpdir, or a tool that
resolves an iCloud-synced path — are **out-of-scope defects**, not ambient
behavior to route around with a broader macOS grant. If you write a role
prompt, hook, or test fixture, scope its filesystem footprint to this
contract explicitly rather than relying on `$HOME`-relative expansion.

**What to click when macOS prompts.** **Deny is always safe.** The daemon's
and sweep children's legitimate working set contains no protected folder, so a
genuine prompt means something reached outside the contract — denying it may
surface a sweep-child failure (a file-not-found / permission-denied on the
out-of-bounds path), which is the **diagnostic signal**, not a bug to route
around. Use that failure to identify and fix the offending script/tool per the
contract above, the same way any other out-of-scope access would be fixed.

**Why Full Disk Access is never the right answer.** FDA (or per-category
Allow) is not the fix even as a convenience, for two independent reasons: (1)
it papers over a real out-of-scope access instead of fixing it, and (2) it
doesn't survive the deployment model. TCC identity is keyed to the binary's
code signature (or, for an ad-hoc/unsigned binary, its cdhash), and
`loom-daemon` is rebuilt from source on every `loom-daemon-update.sh` self-update
roll (#3968) — each rebuild produces a **new** cdhash, so any grant attached to
the previous build silently evaporates. Chasing that with FDA produces a
recurring popup storm *and* a standing over-grant that provides no lasting
benefit. If a grant is ever clicked by mistake, walk it back at System
Settings → Privacy & Security → \<category\> (Photos / Media & Apple Music /
Files and Folders / …) → remove `loom-daemon` — the next self-update rebuild
would have revoked it anyway via the cdhash change, so removing it manually
just does that sooner.

**Ad-hoc code signing (#4016) — pins a legible identifier, does NOT fix TCC
durability.** `loom-daemon-update.sh` and `provision-daemon.sh` ad-hoc-sign the
provisioned binary with a stable `--identifier com.rjwalters.loom-daemon`
(`codesign -f -s - --identifier com.rjwalters.loom-daemon <bin>`, Darwin-only,
best-effort, never fatal). This was **originally proposed** as a way to let a
future, narrowly-scoped TCC grant survive a rebuild — that premise was tested
and found false, and the paragraph above previously repeated the same false
claim. Measured directly (`codesign -dv --verbose=4` / `codesign -d -r-`
before and after applying `--identifier`): TCC keys a grant to the binary's
**designated requirement** (DR), not to the identifier string. A
certificate-signed binary gets an identifier-anchored DR
(`identifier "X" and certificate leaf = H"…"`), which is what would survive a
rebuild — but an **ad-hoc** signature has no certificate chain to anchor to,
so `codesign` always falls back to a **cdhash-only DR**, regardless of what
`--identifier` is passed. Applying the stable identifier makes the identifier
itself constant across rebuilds, but the DR is still `cdhash H"…"`, and that
hash changes on every rebuild — including every self-update roll, since
`build.rs` embeds `LOOM_DAEMON_GIT_COMMIT` / `LOOM_DAEMON_BUILD_TIME` and a
roll by definition follows a `HEAD` move. So the general rule in the FDA
paragraph above — *any* grant on an ad-hoc/unsigned binary is orphaned by the
next rebuild — is unaffected by this change and remains the operative fact.
All this change actually buys is a **legible, stable identifier string**: the
`codesign -dv` / System Settings → Privacy & Security / crash-diagnostic
surfaces show `com.rjwalters.loom-daemon` instead of the rustc `-C metadata`
hash (`loom_daemon-<hash>`, which itself changes on every version bump). It is
also the necessary first step for real signing, which #4244 turns from
speculative into an opt-in capability: set `LOOM_CODESIGN_IDENTITY` (or the
`codesign.identity` config key, env > config > default) to a certificate
already in the keychain and `sign_daemon_binary` signs with that certificate
chain instead of ad-hoc — a certificate-anchored DR survives a rebuild, so a
TCC grant to the daemon identity does too. Unset (or an identity the keychain
doesn't have) falls back to the ad-hoc path above, unchanged, and no new TCC
grant is requested by default. One-time self-signed cert setup (Certificate
Assistant or openssl + `security import`, including the OpenSSL 3 PKCS12
`-legacy` quirk and the `-T /usr/bin/codesign` trust-anchor requirement for
unattended signing) and why grants should target the daemon identity, not
Terminal: [`macos-tcc-codesign.md`](macos-tcc-codesign.md).

### Supervised restart primitive (#4054)

Phase 2 of #4017 (auto-rebuild-and-restart-when-stale). It ships a
manually-triggerable way for the daemon to **end and reliably come back**, so
Phase 3 has a *proven* restart path to call. It deliberately ships **no**
automation — nothing fires it on its own.

```bash
loom-daemon restart          # send RestartDaemon over the IPC socket
loom-daemon restart --reload-supervisor   # launchd only: bootout+bootstrap the job so
                                           # a hand-edited plist's env takes effect (#6682,
                                           # see "Changing daemon environment variables" below)
```

- **Mechanism (macOS):** the plist uses `KeepAlive:{SuccessfulExit:true}` and the
  daemon exits `0` **only** for a `RestartDaemon` request, so launchd relaunches
  the job on that one clean exit and leaves it down on every other (SIGTERM 143,
  SIGINT 130, crash non-zero). The relaunched process comes back with **exactly**
  its start flags/env as launchd already has them bootstrapped in memory — **never
  a fresh read of the on-disk plist file** (see "Changing daemon environment
  variables" immediately below — this is not the widening-safe guarantee it
  sounds like for a hand-edited plist). In-flight sweeps survive (they are
  independent detached processes the daemon never cancels on shutdown) — **this
  is true on launchd only**, see "The two supervisors disagree about in-flight
  work" below. Observable signature: a **new pid**.
- **Supervision proof:** `loom-daemon-start.sh` bakes `LOOM_DAEMON_SUPERVISOR=launchd`
  into the plist, and the daemon ends its process for a restart **only** when that
  var is present. On an unsupervised host (nohup / Linux / `--foreground`) it
  refuses: `loom-daemon restart` prints the refusal, exits non-zero, and the daemon
  keeps running — because nothing would bring it back if it exited (#4017's "log
  loudly, leave the daemon running, do not restart").
- **Why launchd, not `exec` self-replacement:** the design record — including why
  the `exec` (Option 2) and detached-helper (Option 3) alternatives were rejected,
  and the Curator's exit-code-race finding — is in
  `docs/design/supervised-restart-primitive.md`.

#### The two supervisors disagree about in-flight work (#5119)

**"In-flight sweeps survive" is a launchd fact, not a property of the
primitive.** The same exit-0 means opposite things per supervisor, and the ack
now says which one you are on:

| | launchd | systemd `--user` |
|---|---|---|
| Who owns the daemon's children | nobody — they reparent to pid 1 | the **unit's cgroup** |
| A plain `restart`'s effect on them | they keep running (orphaned from the registry, but alive) | the stop job **terminates them** |
| Ack wording | `In-flight sweeps survive by design …` | `WARNING: in-flight sweeps and role runs do NOT survive on systemd …` + the live `N sweep(s)` count + a pointer to `--drain` |

On systemd, `KillMode=mixed` (#4862, in the canonical unit) makes that
termination an immediate SIGKILL of the leftover cgroup members right after the
main process exits; an older `KillMode=control-group` unit instead SIGTERMs the
whole cgroup and SIGKILLs at `TimeoutStopSec`, which additionally sets
`Result=timeout` — and **`Restart=on-success` does not match `Result=timeout`**,
so the relaunch never fires and the host is left daemonless in `failed`. That is
the 2026-08-03 loom-worker-1 outage: three role runs plus a sweep killed, and
~4 minutes with no dispatch until an operator ran `reset-failed && start`.

Both the IPC ack and the daemon's own journal line are composed by the **single**
`ipc::restart_scheduled_message(supervisor, in_flight)` — there is exactly one
place this wording lives, so the reply an operator reads and the line the journal
records can never drift apart (a journal that said "sweeps survive" on a host
whose cgroup had just been reaped is how the 2026-08-03 incident stayed invisible
for four minutes).

Use `loom-daemon restart --drain` (below) whenever in-flight sweeps must
actually finish. It is the *only* variant that preserves them on both
supervisors.

#### Post-restart relaunch verification + self-heal (#5119)

`loom-daemon restart` no longer trusts its own ack. After a `scheduled: true`
reply it:

1. Polls the supervisor's own pid source (`systemctl --user show -p MainPID` /
   `launchctl print`) for a **new, live** pid — `LOOM_DAEMON_RESTART_POLL_SECS`
   (default `30`), interval `LOOM_DAEMON_RESTART_POLL_INTERVAL` (default `1`,
   fractional allowed). A `MainPID=0`, an unchanged pid, or a changed-but-dead
   pid never counts as a relaunch.
2. If the supervisor did not relaunch, dumps its status as a diagnostic and
   self-heals, bounded by `LOOM_DAEMON_RESTART_KICKSTART_POLL_SECS` (default
   `15`):
   - **systemd** — only when `ActiveState=failed` is confirmed:
     `systemctl --user reset-failed <unit> && systemctl --user start <unit>`.
     Any other state refuses to guess (a unit still `activating`/`deactivating`
     would be raced by a `start`).
   - **launchd** — a **plain** `launchctl kickstart <service>`, never `-k`, so a
     job that *did* relaunch during the poll race window is never killed (#4232).
3. Prints the verdict: relaunched on its own, RECOVERED by the self-heal, or
   `RELAUNCH NOT CONFIRMED` (the host may be daemonless, with the manual
   escalation command).

Before scheduling anything on systemd it also warns when the **live** unit still
carries `KillMode=control-group` — a plain restart never re-renders the unit
file, so a host provisioned before #4862 keeps the broken shape indefinitely;
`loom-daemon-update.sh --relaunch` re-renders it.

**The exit code is deliberately unchanged**: it still means "the running daemon
accepted the request", because `loom-daemon-update.sh` and `scripts/loom`'s
`restart` both branch on exactly that (a non-zero sends them down a
refused-restart path whose diagnosis would be wrong). The verification result is
reported on stderr — and the self-heal has already run by the time it prints.
Set `LOOM_DAEMON_RESTART_VERIFY=0` to skip verification entirely;
`loom-daemon-update.sh` keeps its own richer #4232/#4950 verification and
remains the authority on the update path's exit status.

**Residual (documented, bounded): an in-cgroup caller cannot self-heal.** The
verification runs in the *invoking* process. When `loom-daemon restart` is
invoked from **inside the daemon's own systemd cgroup** (most notably the
daemon's autonomous self-update loop), systemd's stop job signals that caller
too, so it may be killed before it can poll or recover. This is not new — the
identical limit applies to `loom-daemon-update.sh`'s own #4950 self-heal for the
same reason — and it is covered out-of-band by `loom-daemon-watchdog.sh`, which
runs from its **own** systemd timer/cgroup and applies the same `reset-failed &&
start` remediation. An operator-invoked restart (an SSH shell, a fleet script) is
outside the cgroup and self-heals in-band, which is the case this issue's
incident actually had.

**Why not `Restart=always`.** It looks like it would fix the `failed`-state trap,
but it inverts the exit-code contract the whole primitive rests on:
`EXIT_SHUTDOWN` (143, an explicit "stay down") and `EXIT_SIGINT` (130) would
start relaunching the daemon an operator deliberately stopped. `Restart=on-success`
plus `KillMode=mixed` plus this self-heal keeps both properties. Raising
`TimeoutStopSec` was likewise rejected — it lengthens the outage instead of
preventing it.

#### Changing daemon environment variables (#4995)

**A plain `loom-daemon restart` preserves the currently-*bootstrapped*
environment, regardless of any on-disk plist edit.** launchd's `KeepAlive`
relaunch reuses the job spec already held in launchd's own memory since the
last `bootstrap` — it does not re-read the plist file from disk. Hand-editing
the installed plist's `EnvironmentVariables` (e.g. adding `LOOM_RUNTIME_JUDGE`)
and then running `loom-daemon restart` silently keeps the OLD value: the
relaunched process comes back with exactly what launchd already had bootstrapped,
not what the file now says. There is no error anywhere — the new/changed env var
is simply not there post-restart.

`loom-daemon restart` detects this automatically: before scheduling anything, it
compares the live (bootstrapped) job's `LOOM_*`/token environment
(`launchctl print gui/<uid>/<label>`) against the on-disk plist's declared
`EnvironmentVariables` (`plutil -convert json`), and prints a loud `WARNING:`
naming every drifted key plus the exact remediation, if they disagree
(`loom_daemon::launchd_env_drift`). The check is advisory-only and never blocks
or fails the restart. No warning normally means the two agree — but it is also
what you get when there was nothing to compare (no launchd job registered under
this label, no on-disk plist) or a probe/parse failed, since every such outcome
is deliberately silent; this is macOS/launchd-specific (systemd re-reads
`Environment=` fresh via `daemon-reload` on every unit reload, so there is
nothing to detect on that path).

**The actual env-change path** is one of:

```bash
# Option A (preferred): the supervised reload primitive (#6682)
loom-daemon restart --reload-supervisor

# Option B: re-render + re-bootstrap in one step (also picks up plist/label/domain changes)
./.loom/scripts/cli/loom-daemon-start.sh

# Option C: bootout the stale bootstrapped job, then bootstrap the edited plist, by hand
launchctl bootout gui/$(id -u)/com.rjwalters.loom-daemon
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rjwalters.loom-daemon.plist
```

All three make launchd re-read the plist file, which a plain `restart` never does.

**Option A — `loom-daemon restart --reload-supervisor` (#6682)** is the
supervised version of Option C: a local `launchctl bootout` immediately
followed by `launchctl bootstrap` against the *already-installed* plist (it
does not re-render anything — pair it with a prior `loom-daemon-start.sh` run,
or Option B directly, if the plist itself also needs regenerating). It exists
because doing Option C by hand is racy: `launchctl bootout` is
**asynchronous** — it returns before the kernel has actually finished tearing
the old job down — so an immediate `bootstrap` can fail with `Bootstrap
failed: 5: Input/output error` even though the plist is perfectly valid,
leaving the job **unloaded with nothing to restart it**. `--reload-supervisor`
settles after the bootout (polling `launchctl print` until the job is
actually gone, bounded by `LOOM_DAEMON_BOOTOUT_SETTLE_SECS`, default 5s) and
retries the bootstrap specifically on that EIO shape (bounded by
`LOOM_DAEMON_BOOTSTRAP_RETRY_ATTEMPTS`, default 4, `LOOM_DAEMON_BOOTSTRAP_RETRY_SECS`
apart, default 2s) — the same retry shape `loom-daemon-start.sh` already
proved in production for this exact race (#5081), now available against an
**already-running** daemon rather than only at start-script invocation time.
`already bootstrapped` (launchd error 37) is treated as a success, not a
failure. If every retry is exhausted, it fails loudly (non-zero exit) and
prints the exact `launchctl bootstrap <domain> <plist>` command to run by
hand — never leaving the job unloaded silently.

`--reload-supervisor` is a **local, launchctl-shelling CLI operation** — it
never talks to the running daemon over its Unix socket at all (the whole
point is bootstrapping a fresh process from a fresh plist read, independent
of anything the currently-running daemon could report), and it refuses
outright — no `launchctl` invocation at all — on a host that is not
launchd-supervised: on systemd, a unit drop-in edit plus a plain `systemctl
--user daemon-reload` already picks up new `Environment=` lines on the next
restart (it re-reads the unit file fresh every time), so there is no
equivalent gap to close there — see "Env keys carried forward across a
re-render" below for how to trigger that restart.

**No drain required first on launchd — but the reverse holds on systemd.**
A launchd bootout tears the job down before bootstrapping it back, but an
in-flight sweep is not killed by it: every sweep gets its own process group
(#3800) and reparents to `launchd` (`ppid=1`) rather than dying with the job
that spawned it — confirmed directly in the incident `--reload-supervisor`
was built to fix. So `--reload-supervisor` needs no `--drain` first, and
never accepts `--drain`/`--abort-drain`/`--then-exit` (it refuses the
combination outright — those flags only make sense against the socket-based
restart request this primitive deliberately bypasses). **This is specifically
NOT true on systemd**: there, `restart` (with or without `--drain`) runs the
unit's stop job over the whole cgroup, which reaps every sweep/role-run child
that has not already reparented out of it (#5119) — so the systemd env-change
path above should always be followed by `loom-daemon restart --drain`, not a
plain `restart`, to finish in-flight work first.

#### Env keys carried forward across a re-render (#4522, #5344)

`loom-daemon-start.sh` re-renders the launchd plist / systemd unit from
**this invocation's own environment** every time it runs — the operator's
exported `LOOM_*` / `GH_TOKEN` / `GITEA_TOKEN` / `FORGE_TOKEN` vars, plus a
few hardcoded values (`PATH`, `HOME`, `LOOM_DAEMON_SUPERVISOR`). A
watchdog-triggered restart, an automated re-render, or a bare re-run from a
different shell can run with a **narrower** env than whatever originally
produced the currently-installed unit/plist.

**Default behavior (#5344): every env key present in the installed
unit/plist is carried forward into the re-render, even when this
invocation's own env no longer has it.** The re-render can only ever be
**equal to or wider** than the installed file — never narrower — unless the
operator explicitly says otherwise. A key this invocation's env is missing
still prints a `WARNING:` naming it (and, for `LOOM_SAFEHOUSE_*` keys, a
migration hint pointing at the `safehouse` block in `.loom/config.json` +
`--from-config`, #4353) so the drift is visible even though the value
survives.

**`--force-env` inverts this**: it acknowledges an *intentional* narrowing
and lets the re-render actually drop whatever keys this invocation's env is
missing, with no warning. Use it when you deliberately want a leaner
unit/plist than what is currently installed (e.g. removing a var for good).

```bash
./.loom/scripts/cli/loom-daemon-start.sh              # default: preserves installed env keys
./.loom/scripts/cli/loom-daemon-start.sh --force-env   # intentional narrowing: drops missing keys
```

To intentionally remove an env key for good, pass `--force-env` once from a
shell that does **not** export it (so nothing carries it forward), then omit
`--force-env` on every subsequent normal re-render/restart.

This is symmetric across macOS (`launchd` `EnvironmentVariables`) and Linux
(`systemd --user` `Environment=` lines) — both install call sites route
through the same `warn_dropped_env_keys` merge. `--print-plist`/`--print-unit`
(read-only inspection, no side effects) preview the same merged result.

#### Scheduled drain-and-restart (`--drain`, #4090)

A plain `loom-daemon restart` exits immediately: on launchd, in-flight sweeps
survive the process boundary but become **orphans** (absent from the relaunched
daemon's in-memory registry — see the "sweeps survive, they are not drained"
amendment above); **on systemd they do not survive at all** — the stop job reaps
the service cgroup (#5119). `--drain` closes both gaps by finishing in-flight work
*before* rolling (and, on systemd, leaving the cgroup empty so nothing is killed):

```bash
loom-daemon restart --drain                       # finish in-flight sweeps, then restart
loom-daemon restart --drain --timeout 600         # bound the wait (default 1800s)
loom-daemon restart --drain --force-after-timeout # at the deadline, cancel stragglers and restart anyway
loom-daemon restart --abort-drain                 # cancel an in-progress drain, resume dispatch (no restart)
```

- **What a drain does:** it sets a daemon-global drain flag that is OR'd into the
  same halt checks the main-health gate uses (#3812) — the work finder stops
  dispatching, the epic supervisor stops scheduling phases, and (newly, #4090) the
  role runner stops **starting** new role ticks. Already-running work is left
  alone. A supervisor task then polls the cross-root in-flight sweep count (every
  managed workspace, not just the primary) and exits `EXIT_RESTART` for the launchd
  relaunch only once it reaches **zero** — so `list_sweeps` after the relaunch is
  consistent with reality and there are **no orphans**.
- **Explicit dispatch is also paused (#5340).** The three autonomous producers
  above read the flag in-process, but `Request::DispatchSweep` — the request type
  behind both the `loom-daemon dispatch` CLI and the MCP `dispatch_sweep` tool —
  used to bypass it entirely: an external caller (a still-running MCP client, a
  script, another host) could keep admitting brand-new sweeps throughout a drain
  window, which was enough on its own to keep the in-flight count from ever
  reaching zero, independent of whether the sweeps present at drain-start were
  simply long-running. Fixed by gating `DispatchSweep` on the same flag
  (`drain_dispatch_refusal` in `loom-daemon/src/ipc.rs`) before it ever reaches the
  registry; `--force`/`force: true` still overrides for an operator who
  deliberately wants a dispatch to land during a drain.
- **Fail-safe timeout:** reaching `--timeout` without `--force-after-timeout`
  **refuses** the restart — it never cancels a sweep, never silently restarts, and
  never silently gives up. `--force-after-timeout` opts into cancelling the
  stragglers via the existing `cancel_sweep` path, then restarts. What happens to
  *dispatch* at that refusal depends on which kind of drain it is (#6007):
  - **A relaunch drain (the version roll)** keeps the roll **pending**: the drain
    flag stays set, so dispatch stays paused, and the supervisor keeps polling and
    fires the restart the instant in-flight reaches **zero** — no operator, no
    re-run, no guessed `--timeout`. Retry windows widen geometrically from the
    requested timeout (`base × 2ⁿ`, capped at 2h each) and the whole sequence is
    bounded by a total paused-dispatch budget of `4 × --timeout` (capped at 4h).
    Once that budget is spent the roll is **abandoned**: dispatch resumes exactly
    as it did pre-#6007, so a wedged sweep can never starve the host of work
    indefinitely, and the note then says to cancel the stuck sweep rather than
    widen the window again. Escape hatches while pending:
    `restart --abort-drain` (give up now, resume dispatch) and
    `restart --drain --force-after-timeout` (cancel the stragglers and roll on the
    next supervisor tick — on a *pending* roll this escalates the active drain in
    place and pulls its re-armed deadline in to now; on a first-attempt drain the
    #4521 pinning still applies).
  - **A then-exit teardown drain** (`fleet drain`'s path) keeps the historical
    behavior byte-for-byte: it clears the flag, resumes dispatch, and stays up —
    `fleet drain` detects that remote refusal by observing `drain.draining: false`
    on a still-reachable daemon and reports its documented exit code `2`.
  Why this asymmetry: on a host that is actually working, resuming dispatch at the
  deadline handed the admission window straight back to the work finder, which
  admitted more sweeps, which made the *next* drain strictly harder to satisfy. In
  the 2026-08-11 fleet roll three of four hosts never activated the new binary for
  exactly this reason (in-flight went `1 → refused → 3`), and the only workarounds
  were an operator-guessed `--timeout 7200` or destroying work with
  `--force-after-timeout`. Both refusal notes (rendered by `loom-daemon status` as
  `Drain: not draining (last: …)`, or on the line under `Drain: DRAINING …` while a
  roll is pending) name the exact local retry —
  `loom-daemon restart --drain --force-after-timeout --timeout <secs>` — so an
  operator is never left guessing at a nonexistent bare `drain` subcommand or the
  unrelated `fleet drain <ssh_host>` remote worker-decommission command (#5340;
  see "`fleet drain`" above — same word, different command, different host).
- **The auto-update loop cooperates rather than racing (#6007).** While any roll is
  armed — including one retained across a refused deadline — `auto_update`'s tick
  skips instead of rebuilding again: the binary is already provisioned and the
  restart is already coming, and a redundant `cargo build` would compete for CPU
  with the very in-flight sweeps the pending roll is waiting on.
- **Supervision proof is checked up front (AC5):** on an unsupervised host the
  request is refused **before** dispatch is paused (`accepted: false`), so a caller
  can detect nothing happened and no silent outage is introduced.
- **Residual (documented, bounded):** role ticks have no sweep-registry entry to
  poll, so a drain stops them *starting* but cannot *await* one already running —
  a drain can complete while a role tick is still mid-flight, bounded by the role
  timeout (`DEFAULT_ROLE_TIMEOUT`, 1800s). Awaiting role ticks is deliberately out
  of scope (it would require a role registry, #4090's stop-and-split boundary).
- **Observability:** `loom-daemon status` renders `DRAINING (n sweep(s) remaining,
  deadline …)` while active and the last transition (timeout refusal / abort)
  afterward; the five `daemon.drain.*` events (above) narrate the transitions on
  the event bus. **Cannot be used for its own first roll** — see the rollout note
  below.
- **Supervised stop/start vs. a full wait-for-zero drain (#5340).** These are two
  different tools for two different situations, not a strict "drain is always
  safer" hierarchy:
  - **Supervised stop/start** (`loom-daemon-stop.sh --restarting` +
    `loom-daemon-start.sh`, or `loom-daemon-update.sh` on launchd's default path)
    does **not** wait for in-flight sweeps and does **not** pause new dispatch
    first — it just stops the process. On **launchd** this is safe for the
    in-flight set: sweep children are independent, reparent to `pid 1`, survive
    the daemon restart by design, and `SweepRegistry::reconstruct` re-admits them
    on the next start (see "sweeps survive, they are not drained" above) — no
    work is lost, only some drain-window telemetry (#5084). This is the right
    choice when you need to roll a binary now and are on launchd.
  - **A full `restart --drain`** is required whenever that reconciliation-on-start
    safety net does not apply: **on systemd**, where sweep/role children live in
    the daemon's own cgroup and a plain stop **kills** them (#5119) — no
    reconciliation possible, because there is nothing left to reconcile; and on
    **any** supervisor when you specifically want zero new dispatch admitted
    during the roll (a drain's whole purpose), not just survival of whatever
    happened to be in flight at the moment of the stop.
  - The default `--timeout` (1800s) is a generic fail-safe, not a per-repo tuned
    value — a host whose sweep lifecycle (Curator→Builder→Judge→Doctor→Merge)
    regularly runs longer than 30 minutes under backlog should pass an explicit
    `--timeout` rather than rely on the default, and should not read a timeout
    refusal on that host as evidence of an admission-gating bug on its own (see
    the explicit-dispatch fix above for the gap that historically *was* a bug).

### Self-update (rebuild + provision + restart, #3968)

The daemon's self-repair loop can file **and fix** its own defects — proven
during the 2026-07-25/26 canary rollout, which produced 16 self-filed daemon
fixes — but every merged fix historically only took effect after an operator
manually rebuilt the Rust binary, reprovisioned it, and restarted the process.
`loom-daemon-update.sh` is the single operator command that closes that gap:

```bash
./.loom/scripts/cli/loom-daemon-update.sh              # detect, rebuild if stale, provision, restart (preserving flags) -- DRAINS FIRST on systemd (see below)
./.loom/scripts/cli/loom-daemon-update.sh --check       # detect only; exit 0 (up to date) / 3 (update available); no writes
./.loom/scripts/cli/loom-daemon-update.sh --dry-run     # print the plan without building/provisioning/restarting
./.loom/scripts/cli/loom-daemon-update.sh --force       # rebuild + provision + restart even if already up to date
./.loom/scripts/cli/loom-daemon-update.sh --no-restart  # rebuild + provision only; leave the running daemon untouched
./.loom/scripts/cli/loom-daemon-update.sh --relaunch    # launchd/systemd only: after a refused restart, re-render the plist/unit + relaunch under supervision (preserves the live LOOM_* env)
./.loom/scripts/cli/loom-daemon-update.sh --drain       # launchd/systemd, Issue #5138: build+provision+restart via `restart --drain` in ONE invocation, no manual second step -- see below
./.loom/scripts/cli/loom-daemon-update.sh --timeout SECS --force-after-timeout  # passthrough to `restart --drain` (only meaningful with an active drain)
./.loom/scripts/cli/loom-daemon-update.sh --restart-now # systemd only, Issue #5138: opt OUT of the drain-by-default below, restart immediately
./.loom/scripts/cli/loom-daemon-update.sh --fetch        # REQUIRE a verified release artifact (never silently fall back to a source build); exit 1 if none resolves
./.loom/scripts/cli/loom-daemon-update.sh --no-fetch     # never consider release artifacts; always build from local source (pre-#5020 behavior)
```

**One-shot drain roll (Issue #5138).** Before this, a drained roll needed the
documented two-step dance — `loom-daemon-update.sh --no-restart` followed by a
separate hand-run `loom-daemon restart --drain` — because the update script only
ever invoked a plain, immediate `restart`. `--drain` (with `--timeout SECS` /
`--force-after-timeout` passthrough) collapses that into the single invocation
above: build, provision, and restart via the exact `restart --drain` primitive
described in "Scheduled drain-and-restart" above, all from one command. **On
systemd, drain is now the DEFAULT** (no flag needed) — an immediate restart there
is destructive, not merely lossy: sweep/role children live in the unit's cgroup,
so a plain restart's stop job can SIGKILL them past `TimeoutStopSec`, landing the
unit in `failed` with `Restart=on-success` never firing (#5119) — a real outage.
`--restart-now` opts back into the immediate/non-drained restart for an operator
who has confirmed nothing is in flight. Launchd's own default is unchanged
(immediate restart, `--drain` opts in) since the launchd failure mode is "only"
lossy telemetry (#5084: an adopted-across-restart sweep never exports
`sweep.completed`/`sweep.outcome`), not a stop-job outage. The fail-safe is
preserved exactly as `restart --drain` implements it: a drain that cannot finish
within its timeout (default/`--timeout`) leaves the **pre-update binary running**
rather than cancelling in-flight sweeps — the script reports this as exit `8`
(distinct from exit `7`'s "restart never took effect" failure), not a silent
success. The `--no-restart` + manual `restart --drain` two-step from before still
works unchanged, for a caller that wants the build/provision and restart steps
kept apart.

#### Artifact-based self-update (epic #4990 Phase 3, #5020)

By default the script now **prefers a prebuilt GitHub Release artifact over a
local `cargo build --release`**, so a CPU-saturated host — or one with no Rust
toolchain installed at all — converges onto the latest release without ever
compiling. This is the consumer side of the release artifacts published by
Phase 1 (#5003) and signed by Phase 2 (#5011).

**Precedence (three-state, `FETCH_MODE`)**

| Mode | How to select | Behavior |
|------|---------------|----------|
| `auto` (**default**) | — | Prefer a verified artifact when one resolves; **softly** fall back to the source build otherwise |
| `force` | `--fetch` / `LOOM_DAEMON_UPDATE_FETCH=1` | Require an artifact; **exit 1** rather than silently building from source |
| `off` | `--no-fetch` / `LOOM_DAEMON_UPDATE_FETCH=0` | Never fetch; always build from local source |

An artifact is used only when the latest release's version is **strictly newer**
than the installed daemon's (an *equal* version wins only under an explicit
`--fetch`; plain `--force` keeps meaning "rebuild this checkout"), the host's
platform maps to one of the release target triples
(`aarch64-apple-darwin`, `x86_64-unknown-linux-gnu`, `aarch64-unknown-linux-gnu`),
and that release actually publishes `loom-daemon-<target>` **and** its
`.sha256`. Everything else — an unrecognized platform, no `gh` CLI, no Releases
(a fork), an unreachable or rate-limited GitHub API, a release with no artifact
for this platform — is a **soft** fallback to the existing source-build path, so
dev machines, canaries, and forks behave exactly as before.

**Verification (epic design principle 2: checksum always, signature when present)**

- **Checksum — unconditional.** The downloaded binary is compared against the
  release's `.sha256`. A mismatch **aborts the whole update (exit 1)** and leaves
  the running daemon untouched; it is never downgraded to a source-build fallback,
  because a mismatch is tamper/corruption evidence, not a resolution failure.
- **Signature — only when present, and never blocking by its absence.**
  - macOS: verified in-binary via `codesign --verify --strict`. "Not signed at
    all" (a release built without the Developer ID secrets) is an expected
    **soft-skip**; "signed but verification fails" is treated as tamper evidence
    and **aborts (exit 1)**.
  - Linux: the detached `loom-daemon-<target>.sig` is verified with
    `cosign verify-blob`, **keylessly by default** (#5054) — see below. If
    `cosign` itself is missing the script emits a **loud skip** and proceeds;
    a verification that actually runs and *fails* **aborts (exit 1)**.

**Linux signature trust root: keyless (Sigstore/OIDC), no distributed key (#5054)**

The release workflow signs Linux artifacts with cosign in **keyless** mode: it
exchanges the workflow's own GitHub Actions OIDC token for a short-lived Fulcio
certificate and publishes that certificate next to the signature as
`loom-daemon-<target>.pem`. Nothing is verified against a long-lived key, so
**no public key is committed to this repo and none needs to be** — a stock
install performs real verification with zero operator configuration.

The **artifact's own shape** selects the mode, never local config:

| Published assets | Mode | Behavior |
|------------------|------|----------|
| `.sig` **+** `.pem` | **keyless** (default) | Verified against the expected *signer identity* + issuer. Failure ⇒ **abort (exit 1)** |
| `.sig` only | key | Needs a resolvable public key (`LOOM_DAEMON_UPDATE_COSIGN_PUBKEY`, `.loom/cosign.pub`, `defaults/cosign.pub`); none resolvable ⇒ **loud skip**; failure ⇒ **abort (exit 1)** |
| neither | — | Soft-skip (absence of a signature never blocks) |

The expected signer identity is **derived** from the release being fetched:

```
^https://github\.com/<owner/repo>/\.github/workflows/[^@]+@refs/tags/<tag>$
   issuer: https://token.actions.githubusercontent.com
```

i.e. *"a workflow in the same repo this artifact came from, running at exactly
this release's tag, with a certificate issued by GitHub Actions."* The workflow
**file** is deliberately not pinned: a future rename of `release.yml` would
otherwise hard-abort updates fleet-wide, while adding no real constraint —
anything that can run a workflow in this repo at this tag can already publish
the release assets. Pin the exact identity with
`LOOM_DAEMON_UPDATE_COSIGN_IDENTITY` if you want the stricter form.

**Why keyless rather than committing `defaults/cosign.pub`.** A committed public
key would also close the loud-skip gap, but it (a) pins the whole fleet to one
keypair, so rotating it silently breaks verification on every host still
carrying the old key, and (b) requires a private key to be provisioned as an
Actions secret before *any* release can be signed at all — which is exactly why
no release had ever been signed: no `COSIGN_PRIVATE_KEY` secret was ever set, so
there was no production public key to distribute in the first place. Keyless
signing needs no secret, so it is on by default for this repo *and every fork*.
A repo that prefers classic key signing sets `COSIGN_PRIVATE_KEY` (which takes
precedence in the workflow) and distributes the matching public key itself; its
releases then publish a bare `.sig` and consumers fall back to the key path
above.

> Keyless verification contacts Sigstore's transparency log, so a host with no
> egress to it will see the verification fail and the update **abort** rather
> than install an unverified binary. That is the intended failure direction:
> the running daemon is left untouched, and `LOOM_DAEMON_UPDATE_FETCH=0` opts
> such a host out of artifact fetching entirely.

**Provisioning and restart are unchanged.** A verified artifact goes through the
*same* `provision_machine_daemon()` seam as a freshly compiled binary (no parallel
provisioning logic), and the same supervised launchd/systemd/pid-file restart
tiers (#4054/#4950). Two artifact-specific adjustments:

- **Post-provision verification** uses `verify_destination_artifact()` instead of
  the source path's embedded-commit compare (a release's commit is unrelated to
  the local checkout's HEAD). It asserts the destination's full `--version` string
  equals the verified artifact's — the same string `provision_machine_daemon()`'s
  version-equality short-circuit compares, so a silent no-op roll still exits `5`.
- **Signing is skipped** for fetched binaries, and `sign_daemon_binary()` now
  refuses to re-sign any binary that already carries a certificate-backed
  signature (`codesign -dvvv` reports an `Authority=`). Without that guard, the
  belt-and-braces ad-hoc re-sign inside `provision_machine_daemon()` would
  silently *downgrade* a Developer ID-signed release artifact to ad-hoc on every
  provision. A locally built binary is always ad-hoc, so this changes nothing for
  the source path.

**Interaction with the autonomous loop's staleness check (worth knowing).** The
loop's own staleness signal is still *embedded commit vs. local source HEAD*. On a
host whose checkout is **ahead of the latest release** (the usual state for a
development host), a newly published release therefore costs one extra roll: the
artifact is fetched and installed first, then the next tick still sees the
installed release commit ≠ local HEAD and rebuilds from source, superseding it.
That is deliberate, converges (the source build's version equals the release's, so
no artifact wins afterwards — there is no ping-pong), and is the right trade on the
hosts this exists for: a saturated host reaches the release *immediately* instead
of waiting out the up-to-6h build-stampede deferral, and a host with no Rust
toolchain simply stays on the release (its source-build attempt fails as retryable
and never displaces it). Hosts that want only source builds can set
`LOOM_DAEMON_UPDATE_FETCH=0`.

**Still requires a source checkout.** This phase changes *what gets installed*,
not the script's entry conditions: `loom-daemon/Cargo.toml` must still exist (the
script also uses the checkout to resolve `origin`'s `owner/repo` slug). Removing
that requirement for binary-only installs is out of scope here.

Concretely, artifact resolution runs *after* the ff-first sync with
`origin/<default-branch>` (#4330), so a checkout that hard-aborts that sync
(genuine divergence, unmanaged dirty tracked files) still exits `2` before any
artifact is considered — pass `--allow-stale` to skip the sync entirely and go
straight to resolution, or fix the checkout. This is unchanged pre-existing
behavior, not something the fetch path introduces, but it does mean "no Rust
toolchain" is not the same as "no git checkout hygiene".

**Release-cadence gap visibility (#6010).** Releases are cut deliberately
less often than `VERSION` bumps (see
[`release-cadence.md`](release-cadence.md) for the policy), so the newest
release can legitimately sit behind the current source tree for a while — the
pre-#6010 "not newer than the installed version" message alone did not make
that distinguishable from "you're already up to date". The script now also
compares the resolved release against the **source tree's own `VERSION`
file** and reports it on every path that resolves a release (plain run,
`--check`, and a forced `--fetch`'s hard-fail reason), so an operator can tell
*before* running `--fetch` whether it can currently reach source at all.

**Extra environment variables** (all optional; the first three are primarily test
seams):

| Variable | Purpose |
|----------|---------|
| `LOOM_DAEMON_UPDATE_FETCH` | `1`/`true`/`yes` ⇒ force (`--fetch`); `0`/`false`/`no` ⇒ off (`--no-fetch`); unset ⇒ auto |
| `LOOM_DAEMON_UPDATE_GH_REPO` | Override the `owner/repo` slug used for release resolution (default: parsed from the `origin` remote) |
| `LOOM_DAEMON_UPDATE_TARGET` | Override the detected release target triple |
| `LOOM_DAEMON_UPDATE_COSIGN_PUBKEY` | Path to the cosign public key used to verify a **key-signed** Linux `.sig` (one published without a `.pem`) |
| `LOOM_DAEMON_UPDATE_COSIGN_IDENTITY` | Pin one exact expected keyless signer identity instead of the derived regexp |
| `LOOM_DAEMON_UPDATE_COSIGN_OIDC_ISSUER` | Expected keyless certificate issuer (default `https://token.actions.githubusercontent.com`) |

**No new daemon config keys.** The autonomous self-update loop
(`autonomous.autoUpdate.*`) needs no change and passes no fetch flag: it invokes
`loom-daemon-update.sh --no-restart` and inherits the `auto` default, which is
exactly the unattended behavior it wants (prefer an artifact, degrade to a
rebuild). It deliberately does **not** pass `--fetch`, since that would turn a
transient GitHub API hiccup into a failed roll. The existing exit-code contract is
unchanged: a checksum/signature failure maps to exit `1` (retryable, same bucket
as a failed `cargo build`), so `classify_exit` needs no new cases.

**Reused by `fleet add-worker`'s initial provisioning (epic #4990 Phase 4,
#5067).** `fleet add-worker`'s `machine-layout` step (see the `fleet add-worker`
section above) shells out to this same script (`--no-restart`, since no daemon
is running yet on a brand-new worker) instead of duplicating the fetch/verify/
checksum logic for a second call site — so a new worker with **no Rust
toolchain at all** can go from a bare provisioned OS to a running daemon using
only a release artifact, the same `auto`-mode precedence described above.

**Launchd refused-restart fallback (`--relaunch`, exit 6, #4118)**: on the
FIRST roll onto a #4077-capable binary the *running* (old) daemon has no
`RestartDaemon` handler and refuses the `loom-daemon restart` IPC, so the script
exits **6** rather than reporting a half-update. It does **not** tell you to
`launchctl bootstrap` the existing plist — that plist is stale by construction
(no `KeepAlive:{SuccessfulExit:true}`, no `LOOM_DAEMON_SUPERVISOR`), so
bootstrapping it relaunches *unsupervised* and every subsequent roll refuses
identically forever. (A bare `launchctl bootout` of that plist no longer kills
in-flight sweeps on a current build, #5081 — every sweep is spawned in its own
process group, `process_group(0)` since #3800, which bootout's job-tree
teardown does not reach, so it reparents to pid 1 and keeps running; this
corrects guidance that was accurate before #3800 but had gone stale.) Instead,
`--relaunch` (or `LOOM_DAEMON_UPDATE_RELAUNCH=1`) re-renders the plist via
`loom-daemon-start.sh` — installing both supervised keys — while
**preserving the live plist's `LOOM_*`/token `EnvironmentVariables`** (read with
`plutil` + `jq`, `PATH`/`HOME`/`LOOM_DAEMON_SUPERVISOR` excluded so autonomy
flags never silently narrow to FLAGS-OFF, #4011), and stops the old daemon
**gracefully with `SIGTERM`** rather than a bootout (belt-and-braces against a
double teardown, since `loom-daemon-start.sh`'s own launchd block below
bootouts the loaded job again before re-bootstrapping). The default path stays
exit-6 (no `--relaunch`) so a plist-env-changing relaunch is always a
consented action. `loom-daemon-start.sh`'s bootout+bootstrap sequence itself
settles after bootout, retries the bootstrap step if it hits the async
bootout/bootstrap race (`Bootstrap failed: 5: Input/output error`, #5081 —
`bootout` returns before the kernel finishes tearing the job down, so an
immediate `bootstrap` can fail even against a valid plist), and verifies the
relaunched job's live pid **and** reported `EnvironmentVariables` before
reporting success — see `LOOM_DAEMON_BOOTOUT_SETTLE_SECS` /
`LOOM_DAEMON_BOOTSTRAP_RETRY_ATTEMPTS` / `LOOM_DAEMON_BOOTSTRAP_RETRY_SECS` in
`loom-daemon-start.sh --help`.

**Staleness detection** is primary-local, zero-network: it compares the git
commit **baked into** the currently-resolved `loom-daemon` binary (embedded at
build time via `build.rs` → `LOOM_DAEMON_GIT_COMMIT`, the same value folded
into `loom-daemon --version`) against the **local source tree's** current
`HEAD` short commit — directly answering "would rebuilding right now produce a
different binary?". Separately, and purely **advisory** (it never gates the
rebuild decision), the script bounded-fetches `origin/<default-branch>` and
warns when local `HEAD` itself is behind, mirroring
`check-main-freshness.sh`'s pattern — so a cron-scheduled run distinguishes
"you're current with local HEAD" from "local HEAD is itself stale".

**Flag preservation (the FLAGS-OFF/opt-in contract, never widened)**:
`loom-daemon-start.sh` now persists its resolved invocation flags to
`.loom/.daemon.flags` (gitignored, one flag per line) on every start attempt —
`--foreground`/`--help` are filtered out (script-only, not autonomy state);
`--from-config`, `--work-finder`, `--health-gate`, `--no-work-finder`,
`--no-health-gate` are kept verbatim. `loom-daemon-update.sh` reads this file
and replays it **exactly** on restart — a daemon started bare (FLAGS-OFF)
restarts bare; a daemon started `--work-finder` restarts `--work-finder`,
never gaining `--health-gate` it didn't have. A missing flags file (a daemon
started before #3968, or manually) falls back to a bare FLAGS-OFF restart
rather than guessing, with a loud warning.

**A daemon that was NOT running is never started.** If `.loom/.daemon.pid`
has no live process at update time, the script rebuilds and provisions but
prints "was not running — nothing to restart" and stops — it never widens the
system state by starting autonomy (or anything) that wasn't already running.
Combined with the "in-flight sweeps survive a stop" shutdown decision above,
a rebuild-and-restart window never kills active dispatched work and never
silently upgrades a stopped daemon into a running one.

**Provisioning** targets wherever the resolved binary lives: an explicit
`LOOM_DAEMON_BIN` override is provisioned in place; otherwise the fresh binary
is installed to the machine-level location via
`scripts/install/provision-daemon.sh`'s `provision_machine_daemon` (default
`~/.local/bin/loom-daemon`, override `LOOM_DAEMON_BIN_DIR`) — the same
convention `loom-daemon-start.sh` already resolves through `command -v
loom-daemon`.

**Self-verifying rebuild → provision (a roll can never silently ship nothing,
#4053)**: the rebuild → provision path proves it produced what it claims, so a
successful-looking run can no longer install a stale binary:

- **Built-commit verification (before provisioning).** After `cargo build
  --release`, the script asserts the freshly-built binary's embedded commit
  (parsed from `--version`) equals the source `HEAD` it was built from. On
  mismatch it **fails loudly and does not provision** — a build that succeeds
  yet bakes in the wrong commit is a build-system defect (historically a
  `build.rs` `cargo:rerun-if-changed` watch-set bug that missed HEAD movement),
  not a compile failure, and retrying cannot fix it. **Exit code `4`**,
  distinguishable from a compile failure (`1`). The underlying `build.rs`
  watch set is now correct-by-construction via `git rev-parse --git-path`
  (resolving `HEAD`, the current branch's ref, and `packed-refs`), so it tracks
  HEAD movement in the main checkout, a **linked worktree** (`.git` is a file —
  every Builder's environment), a detached HEAD, and a packed-refs repo alike.
- **Post-provision verification (after provisioning).** The script then asserts
  the **destination** binary's `--version` is the expected build — for both the
  `LOOM_DAEMON_BIN` override path and the machine-level
  `provision_machine_daemon` path. This is the direct answer to "reports success
  while shipping nothing": the `--version`-equality short-circuit in
  `provision-daemon.sh` is retained (it is correct once the rebuild is correct)
  but can no longer produce a silent no-op on a real roll — `provision_machine_daemon`
  exports the destination it wrote to (`PROVISIONED_DAEMON_BIN`, set even on the
  short-circuit path) so the caller can verify it. A destination that is not the
  expected build, or a provisioning step that reports failure, now **exits
  non-zero** (`5` for a destination mismatch) instead of the pre-#4053 soft
  warn that left the exit code at `0`.

**Read-only "update available" surface (`loom-daemon status`)**: separately
from the update script, `loom-daemon status` / `loom-daemon status --json` now prints
a purely local, read-only self-update line — the same built-commit-vs-source-HEAD
comparison, computed in-process (`self_update::check()`) with at most one `git
rev-parse` subprocess and zero network calls. It never triggers a rebuild or
restart on its own; it is advisory-only, matching the required "no auto-restart
without opt-in" contract. Example:

```
Self-update: built from ab12cd3 — UPDATE AVAILABLE (source checkout HEAD is de45f67); run `./.loom/scripts/cli/loom-daemon-update.sh` to rebuild + provision + restart
```

`loom-daemon-update.sh` requires an actual Loom source checkout
(`loom-daemon/Cargo.toml` must exist) and refuses to run against a binary-only /
release-tarball install — even when it ends up installing a prebuilt release
artifact rather than rebuilding (see
[Artifact-based self-update](#artifact-based-self-update-epic-4990-phase-3-5020)).

**Running-vs-disk build staleness (#5341) — a distinct comparison from the
`Self-update:` line above.** The `Self-update:` line answers "would rebuilding
right now produce something different?" (this binary's baked-in commit vs. the
**source checkout's** HEAD). It does NOT answer "is the long-running daemon
PROCESS I'm talking to the same build as the file on disk?" — and those two
can disagree: rebuilding the disk binary without restarting the daemon leaves
an OLDER process still running behind a NEWER file. Because `--version` and
every fresh `loom-daemon status` invocation exec the on-disk binary, both
historically reported the disk build even when querying a stale running
daemon — the daemon silently misreported its own currency, and every routine
staleness check (including `Self-update:` itself, run from the same on-disk
binary) passed. On `loom-worker-1` (2026-08-04) this was invisible except by
comparing `ps -o lstart` (process start time, ~25h old) against the disk
binary's `stat` mtime (~1h old) by hand.

`loom-daemon status` (human and `--json`) now prints a `Build: …` line,
unflagged, in the same block as `Protection:` / `Observability:` — comparing
the **running daemon process's** build (sourced over IPC from the process
itself, never re-read from disk) against **this CLI invocation's own**
compile-time build (which, because it is a fresh exec of the on-disk binary,
IS the disk build):

```
Build:         STALE — running 5111b74a (built 2026-08-03T02:09:51Z), disk 3f5132a5 (built 2026-08-04T01:00:12Z) — restart to roll: ./.loom/scripts/cli/loom-daemon-stop.sh && ./.loom/scripts/cli/loom-daemon-start.sh
```

`--json` carries the same facts under `daemon_build`
(`running_commit`/`running_built_at`/`disk_commit`/`disk_built_at`/`stale`).
`--version` itself is **not** fixed by this — it is clap's own built-in flag,
answered before any subcommand logic (and therefore any IPC round-trip) runs,
and it is also the only way to inspect a binary before a daemon is even
started — so it necessarily continues to report the ON-DISK build only; use
`loom-daemon status` when you need the running process's own build.

### Autonomous self-update loop (#4055)

Phase 3 of #4017 closes the self-repair cycle end to end: when enabled, the
daemon **rebuilds and restarts itself** onto a fresher binary without operator
action, instead of only surfacing the read-only "update available" hint above.
It is the *deciding + sequencing* layer — it reuses `loom-daemon-update.sh`
(driven with `--no-restart`) for the rebuild/provision and the #4090 drain
primitive for the restart, reimplementing neither.

**Opt-in, default OFF** (it has side effects on the running process). Enable via
`autonomous.autoUpdate.enabled` / `LOOM_AUTO_UPDATE=1`; tune the cadence, settle
window, and stampede-gate deadline with `intervalSecs` (default 900) /
`settleSecs` (default 600) / `deferDeadlineSecs` (default 21600). All four knobs
resolve **env > config > default** through `config_resolver`, so the
`.loom-project/` tier is honored like every other `autonomous.*` block.

**Exactly one loop per daemon process** — not a `spawn_multi_*` per-workspace
fan-out. Its subject is the daemon process itself (one binary, one source
checkout, one restart), so fanning it out across N registered workspaces would
race N `cargo build`s in one tree and N restarts of one process. Config is read
from the daemon's default workspace; the in-flight count (gate 4) is inherently
cross-root.

Each tick (surfaced in `loom-daemon status` — human and `--json` — as
`auto_update` last-check / last-roll / backoff / terminal fields, all
`#[serde(default)]` wire-compatible):

1. **Staleness** — `self_update::check()`. Only `update_available == Some(true)`
   is actionable; `Some(false)` (current) and `None` (tarball / no checkout /
   `BUILT_COMMIT == "unknown"`) **never** rebuild.
2. **Clean-tree gate** — refuses to build unless the source working tree is
   clean (`git status --porcelain` empty). `CARGO_MANIFEST_DIR` is the operator's
   live checkout, so building it dirty would compile uncommitted work into the
   daemon. It **never** runs `git pull`.
3. **Settle window** — waits `settleSecs` after first observing a stale commit,
   resetting on every further commit, so a burst of daemon merges collapses into
   a single roll.
4. **Build-stampede gate (bounded, #4929)** — defers the rebuild while
   `ipc::count_in_flight_sweeps` reports any non-terminal sweep across every
   managed root (a `cargo build --release` competes with in-flight sweep builds
   for CPU). The deferral is **not** open-ended: a host that runs at its
   dispatch cap around the clock never reaches zero in-flight sweeps, and an
   unconditional gate there starved the roll indefinitely (`last_roll: null`
   with `update_available: true` for a day+). After `deferDeadlineSecs` of
   *continuous* deferral the loop rebuilds anyway, niced to 19 so the forced
   build yields CPU to the running sweeps; the roll then proceeds through the
   same drain path below (and if that drain is refused or times out, the fresh
   binary is still provisioned for the next supervised restart). The clock
   re-arms on any zero-in-flight check, a new source commit, or a completed
   rebuild, so short busy bursts never reach the deadline.
5. **Roll via drain, not a bare restart** — on a clean rebuild it triggers
   `ipc::handle_drain_request` (#4090), so in-flight sweeps finish first and
   survive in the registry rather than being orphaned as bare processes. The
   restart exits into launchd `KeepAlive:SuccessfulExit`, which relaunches from
   the plist's persisted `ProgramArguments`/`EnvironmentVariables` — so the
   daemon comes back with **exactly its prior autonomy flags, never wider** (see
   [Scheduled drain-and-restart](#scheduled-drain-and-restart---drain-4090)).
6. **Backoff + terminal state** — a retryable build failure (script exit `1`,
   spawn/timeout) backs off exponentially (60s → … → 3600s ceiling); a
   commit-identity / build-verification mismatch (#4053 exit `4`/`5`) is
   **terminal** — surfaced in `loom-daemon status`, not retried until the source commit
   advances. A successful roll resets the counter.

### End-to-end acceptance playbook

The goal state — "file a `loom:triage` issue, watch it build" with zero operator
dispatch — is validated by the E2E playbook at
[`docs/autonomous-mode-e2e.md`](https://github.com/rjwalters/loom/blob/main/docs/autonomous-mode-e2e.md)
(upstream Loom repo — not shipped to consumer installs): it walks a
throwaway issue from `loom:triage` → Curator → `loom:issue` → work-finder
dispatch → PR → merge, with a scripted label-transition assertion, and confirms
the operator only ever created the issue.

## Observability exporter (`observability`, #4705, epic #4702 Phase 1)

An **opt-in, off-by-default** telemetry exporter that pushes fleet telemetry
(sweep lifecycle/outcome, token-pool snapshots, host health) to a configured
HTTP(S) sink. This is the daemon-side half of epic #4702's rich fleet
dashboard; the Phase-2 Cloudflare Workers backend (#4725) is the first real
sink. The exporter ships value standalone, with no cloud dependency: a
disabled or under-configured `observability` block is a zero-syscall no-op,
exactly like `autonomous.idleExit` above.

**Standing up your own sink**: the Workers backend is a deploy-to-your-own-
account template — `wrangler.toml` plus a runbook that walks account/D1
setup, migrations, ingest-key generation and rotation, and pointing the
`observability` block below at the result, with a companion guide for gating
the authenticated view behind Cloudflare Access. Both live beside the backend
in the upstream Loom repo (not shipped to consumer installs):
[`dashboard/docs/deploy-runbook.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/deploy-runbook.md)
and
[`dashboard/docs/cloudflare-access.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/cloudflare-access.md).

### Config surface (`.loom/config.json → observability`)

```json
{
  "observability": {
    "enabled": false,
    "endpoint": "https://ingest.example.com/v1/telemetry",
    "batchSize": 50,
    "flushIntervalSecs": 30,
    "queueCapacity": 2000
  }
}
```

Precedence is **env > config > default**, same convention as every other
`autonomous.*`-style block: `LOOM_OBSERVABILITY_ENABLED` /
`LOOM_OBSERVABILITY_ENDPOINT` / `LOOM_OBSERVABILITY_INGEST_KEY_FILE` /
`LOOM_OBSERVABILITY_BATCH_SIZE` / `LOOM_OBSERVABILITY_FLUSH_INTERVAL_SECS` /
`LOOM_OBSERVABILITY_QUEUE_CAPACITY` override the matching config key, which
overrides the built-in default (`false` / *(none)* / `$HOME/.loom/observability/ingest.key`
/ `50` / `30` / `2000`). The **ingest key is never inline in config** —
`ingestKeyFile` names a path the daemon reads once at startup; the key
contents never appear in a log line or error message (only the *path* does),
and requests authenticate via an `Authorization: Bearer <key>` header.
`loom-daemon` refuses to export (logs a `WARN` and skips both background
tasks) when enabled without a resolvable `endpoint` or a readable, non-empty
`ingestKeyFile` — treated the same as "not configured", not a startup
failure.

`ingestKeyFile` is deliberately **excluded** from the example above and must
never be committed to a shared `.loom/config.json`: it is the one key in this
block that is host-specific by definition, and its default already resolves
per-host (`$HOME/.loom/observability/ingest.key`) with zero config needed. A
host that commits an explicit value bakes in that host's own home directory
for every other host that shares the config file — issue #5336, where a
macOS path landed unreadable on a Linux worker. A non-default path belongs in
the gitignored `.loom-local/local.json` override tier or
`$LOOM_OBSERVABILITY_INGEST_KEY_FILE`, never the committed file. Full
guidance: [`.loom/docs/observability.md`](observability.md) §1.

### Architecture

Two background tasks, spawned together (`observability::spawn_task`,
`loom-daemon/src/observability/`) alongside the `idle_exit`/`auto_update`
wiring in `main.rs`:

- **Collector** (`observability::collector`) — a pure [`EventBus`](#event-taxonomy-frozen-for-v0100)
  subscriber (`sweep.global.dispatch` + `sweep.issue.*`, adding **no new emit
  call sites** anywhere else in the daemon, the same design
  [safehouse](#completion-narration--public-fleet-feed-4426) narration
  uses) that maps lifecycle events to the versioned
  `loom-daemon/src/telemetry/` schema (#4703) — `sweep.started` /
  `sweep.phase` / `sweep.completed` / `sweep.outcome` — resolves the emitting
  repo's `owner/repo` slug and public/private visibility tag
  (`telemetry::visibility`, an existing `gh api repos/{owner}/{repo}
  --jq .private` TTL-cached probe, private-by-default on any failure), and
  every 5 minutes additionally samples host-level `tokens.snapshot` (from the
  resolved token pool's `.ranking` file) and `host.health` (CPU idle
  fraction, load-per-core, logical CPU count, worktree-root free space,
  daemon version/uptime) records, which have no corresponding bus event.
- **Sender** (`observability::sender`) — drains the queue on a jittered
  `flushIntervalSecs` timer via `observability::exporter::HttpsExporter`
  (`reqwest`, JSON array POST per batch), retrying a failed batch with
  jittered exponential backoff (5s floor, 5 minute ceiling) — jitter comes
  from this crate's existing dependency-free `tokens_pool::rng::Rng`, not a
  new `rand` dependency.

Between them sits `observability::queue::DurableQueue` — a bounded,
disk-backed FIFO (`<workspace>/.loom/logs/observability-queue.jsonl`,
override `LOOM_OBSERVABILITY_QUEUE_PATH`) that survives a clean daemon
restart across host sleep/idle-shutdown (#4467/#4697): every push/ack
atomically rewrites the file (temp-file + rename, mirroring
`idle_exit::write_marker`). Once `queueCapacity` is reached, the **oldest**
queued record is dropped and a running `dropped_total` counter is logged —
bounded growth, never an unbounded queue, and never a silent loss.

### Exporter trait boundary (Phase 4 forward-compat, no OTel dependency yet)

`observability::exporter::Exporter` is a two-method trait (`emit_batch`,
`flush`) `observability::sender`'s drain loop is generic over — `HttpsExporter`
is its only implementation today. This issue adds **no OTel dependency**; the
trait boundary exists so epic #4702 Phase 4's optional OTLP exporter (for
operators with an existing Grafana/Honeycomb/self-hosted-collector stack) can
be a second `impl Exporter` later without touching the queue or sender.

### Read-only invariant

The exporter only ever originates outbound HTTP POSTs; it never parses a
response body for anything beyond a batch-accepted/rejected status, and
nothing received over this channel ever mutates daemon state — steering
(dispatch/cancel/pause) stays with MCP, per the epic's explicit scope.

## Fleet dashboard (`loom-daemon serve`)

`loom-daemon serve` (#4329 phases 1-3: #4391 status snapshot, #4392 SSE event
tail, #4393 page rendering) starts a **separate, opt-in** read-only HTTP
listener that proxies the running daemon's existing Unix socket for
browser-based fleet visibility. It is not part of the normal daemon-run path —
a daemon started without `serve` never opens this port, and `serve` itself
never mutates daemon state or the sweep registry; every request re-fetches a
live snapshot over IPC.

### Starting it

```bash
loom-daemon serve                                   # http://127.0.0.1:7420/
loom-daemon serve --port 9000                        # non-default port, still loopback-only
loom-daemon serve --peers http://host2:7420,http://host3:7420   # multihost fleet view
```

| Flag | Default | Purpose |
|------|---------|---------|
| `--port` | `7420` | TCP port for the HTTP listener |
| `--bind` | `127.0.0.1` | Interface address to bind |
| `--allow-non-loopback` | off | Required opt-in to bind any non-loopback `--bind` address |
| `--peers` | `""` (empty) | Comma-separated peer `serve` base URLs, served verbatim at `GET /api/peers` for client-side fan-out |

### Security posture (localhost-only invariant)

- **Off by default**: nothing listens on any port unless `loom-daemon serve`
  is explicitly invoked — a plain `loom-daemon` run (autonomous or not) never
  opens this listener.
- **Binds `127.0.0.1` by default.** A wildcard/unspecified bind (`0.0.0.0` /
  `::`) is refused **unconditionally**, even with `--allow-non-loopback` —
  this endpoint can never become reachable from the public internet, only
  from an explicit interface address.
- A non-loopback `--bind` (e.g. a tailnet interface address, for cross-host
  fleet visibility) requires the *separate* explicit `--allow-non-loopback`
  flag — the bind address alone is never enough to opt in.
- **Read-only, stateless, no steering endpoints.** Every route is a bare
  `GET`; any other method gets `405`. The SSE bridge only ever sends the
  IPC `SubscribeEvents` request — it never publishes an event and never
  writes to the daemon socket beyond that one subscribe call, so nothing
  this listener originates can appear on the bus. Nothing is written to
  disk. There is no dispatch/cancel/restart/steering route of any kind.
- Every response carries a permissive `Access-Control-Allow-Origin: *`
  header (required for the dashboard's client-side peer fan-out); this is
  judged safe specifically because the surface is read-only, carries no
  credentials, and is loopback-by-default — the bind posture is the
  access-control layer, not CORS.

### What it serves

| Route | Content |
|-------|---------|
| `GET /` | The embedded single-page dashboard (plain HTML/CSS/vanilla JS, no build toolchain, compiled into the binary) |
| `GET /api/status` | JSON status snapshot: the same `DaemonStatusReport` `loom-daemon status --json` aggregates, flattened with a `hostname` field |
| `GET /api/events` | `text/event-stream` (SSE) tail of the daemon's event bus |
| `GET /api/pipeline` | Forge-side queue counts per managed repo (same source `status --pipeline` uses), fronted by a 20s in-process cache |
| `GET /api/tokens` | Per-account rows (name / status / 5h utilization) read from the resolved token pool's `.ranking` file |
| `GET /api/peers` | The configured `--peers` list, verbatim — this daemon never fetches a peer itself; the browser fetches each peer's own `/api/status`/`/api/events` directly |

The dashboard page renders: the in-flight sweep registry, the dynamic
concurrency cap/capacity breakdown, per-token usage bars, the per-repo
main-health gate state, per-repo pipeline queue counts, configured fleet
peers, and a live event tail — each panel backed by one of the routes above.

### Event tail topics

`GET /api/events` subscribes to the six frozen `sweep.*` topics from the
[event taxonomy](#event-taxonomy-frozen-for-v0100) above — `sweep.issue.{N}.phase`,
`.blocker`, `.exited`, `.crashed`, `sweep.global.dispatch`, and
`sweep.global.completed` — through the exact same `SubscribeEvents` IPC
request and `event_bus::topic_matches` prefix filter every other subscriber
uses. It additionally includes the already-authorized `epic.issue.*`,
`daemon.capacity.advisory`, `daemon.drain.*`, and
`daemon.dispatch.headroom_advisory` topics, clearly separable from the
frozen six because every SSE frame carries its exact source topic in the
JSON payload (never renamed or re-namespaced). There is no replay buffer:
a reconnecting client (`EventSource`'s built-in retry) resumes from live,
never from history.

## Locks and lifecycle

Each dispatched sweep acquires a directory lock under
`.loom/locks/issue-<N>/` via `mkdir` (POSIX-atomic). The lock dir
contains an `owner.json` with the dispatching daemon PID and the sweep
ID. The reaper releases the lock when a child dies; `cancel_sweep`
releases it explicitly. On daemon startup, `SweepRegistry::reconstruct`
admits live-lock owners back into the registry and drops stale locks
whose owner PID is dead.

### Live-claim guard (#4556)

Lock *existence* is not the same question as "is a sweep for this issue
running right now?", and every re-dispatch path answers the first while
needing the second. Issue #4275 was dispatched **seven times in 77
minutes** onto one shared worktree because each path first convinced
itself the sweep was dead — the claim reconciler on a dead-looking
recorded PID, the mid-build watchdog on a terminal registry entry, the
review-stall watchdog on a silent log — and *released the lock or
reverted the label before dispatching*, so the existence check had
nothing left to collide with. Three further dispatches came from a
second `loom-daemon` on the same host, which shared neither the first
daemon's memory nor its `.loom/locks/`.

`live_claim::probe` answers the stronger question, read-only, with three
short-circuiting evidence legs — a live `.loom/locks/issue-<N>/owner.json`
owner, a live record in the machine-level `~/.loom/sweeps.json` journal
(matched across related repo roots, so a daemon running out of a nested
worktree and one running out of the parent checkout see each other), and
a `/proc` scan for a live `/loom:sweep <N>` process whose cwd is inside
the workspace. It gates four places:

| Site | Refusal |
|---|---|
| `SweepRegistry::dispatch` (step 2.9) | `LiveClaimDispatchError`, before any lock, label write, or forge call — covers all six dispatch routes, including both watchdogs |
| `midbuild_watchdog_once` | refuses **up front**, because that path `git reset --hard`s the shared worktree and burns its single retry *before* it calls `dispatch` |
| `release_lock_owned` | `LockReleaseOutcome::HolderAlive` — the caller's dead-sweep verdict was wrong, so the lock stays and the label restore / re-dispatch are skipped |
| `claim_reconciliation` | vetoes a `Reclaim`, so `loom:building` is not reverted out from under a working sweep |

In `midbuild_watchdog_once` the probe **must** stay ahead of
`claim_lock_for_midbuild` (#4564/#4602). That call takes the issue lock
over *in place*, rewriting `owner.json` to the daemon's own PID and a
`midbuild-watchdog-…` sweep id, and it does so precisely in the
false-dead case this guard exists to catch. Probing afterwards would
read the watchdog's own record instead of the live sweep's (silencing
leg 1), and the refusal path returns without releasing, so the live
sweep's claim record would stay clobbered and its own `release_lock_owned`
would then read `Superseded` and skip its label restore.
`midbuild_live_claim_probe_runs_before_the_lock_takeover` pins the order
by byte-comparing `owner.json` across a refused tick.

Fail-open throughout: a missing/corrupt `owner.json`, an unreadable
journal, a zombie PID, or a recorded PID whose argv does not name this
issue's sweep (a recycled PID) all resolve to "no evidence", so a stale
file can never wedge an issue. Every refusal logs at `WARN`; the
work-finder counts a refusal as an in-flight skip, not an error.

Integration tests spawn the daemon with its machine-level state confined
to a fixture (`isolate_daemon_state`: `LOOM_WORKSPACES_PATH`,
`LOOM_SWEEPS_JOURNAL_PATH`, `LOOM_WATCHES_PATH`, `LOOM_WATCH_RESULTS_LOG`,
plus the cwd), so a test daemon has no managed workspace to reconcile or
dispatch against.

## What this page does NOT describe

The legacy schema and tuning advice that historically lived here — the
Python `daemon-state.json` schema, `MAX_SHEPHERDS`/`ISSUE_THRESHOLD`
tunables, work-generation cooldowns, `shepherd-N` pool sizing — described
a Python brain that no longer exists. **None of that exists post-v0.10.0.**

- The daemon **does not** generate work on a cadence. The one opt-in
  exception is the role runner's idle edge: `onIdle: ["architect"]`
  fires a capped proposal pass when a repo's backlog empties (#5656).
  Interval-cadence Architect and Hermit work generation remains out of
  scope, tracked under follow-up #3381.
- The daemon **does not maintain a shepherd-N pool**. Each issue
  detaches its own `claude -p "/loom:sweep N"` child; concurrency is
  bounded by the daemon's dispatch handling and is operator-controlled
  via separate `dispatch_sweep` MCP calls.
- The daemon **does not track** `pipeline_state`, `warnings`,
  `completed_issues`, or `last_*_trigger`. The forge is the source of
  truth for pipeline state.
- Support roles run as **cron-driven GitHub Actions workflows**, not as
  long-running daemon-managed processes. There is no `JUDGE_INTERVAL`
  or `CHAMPION_INTERVAL` to tune from daemon config.

The decision to delete rather than re-implement the legacy state file
is documented in `docs/migration/daemon-state-consumers.md` §"Conclusion:
what Phase 3 deletes vs preserves".

## Related resources

- **Architecture epic**: [#3449](https://github.com/rjwalters/loom/issues/3449)
  (rebuild of the daemon backend).
- **Phase A** (dispatch surface): #3452 / PR #3459.
- **Phase B** (event bus): #3453 / PR #3460.
- **Phase C** (monitoring + subscription tools): #3455.
- **Migration guide**:
  [`docs/migration/v0.10.0-shepherd-deprecation.md`](https://github.com/rjwalters/loom/blob/main/docs/migration/v0.10.0-shepherd-deprecation.md)
  (upstream Loom repo — not shipped to consumer installs).
- **Config resolution layer** (#4039, Epic #3835 Phase 2): a single resolver
  over private defaults + tracked `.loom-project/project.json` + ignored
  `.loom-local/local.json` + legacy `.loom/config.json`, additive-only (no
  existing call site — including this doc's `.loom/config.json` references
  above — has been migrated onto it yet; see follow-up #4047). Schema,
  precedence, and how it composes with `env > config > default`:
  [`docs/design/config-resolution-tiers.md`](https://github.com/rjwalters/loom/blob/main/docs/design/config-resolution-tiers.md)
  (upstream Loom repo — not shipped to consumer installs).
- **Source** (upstream Loom repo — not shipped to consumer installs):
  - [`loom-daemon/src/types.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/types.rs) — IPC types.
  - [`loom-daemon/src/sweep_registry.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/sweep_registry.rs) — registry + reaper.
  - [`loom-daemon/src/event_bus.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/event_bus.rs) — pub/sub bus.
  - [`loom-daemon/src/ipc.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/ipc.rs) — request dispatcher.
  - [`mcp-loom/src/tools/sweeps.ts`](https://github.com/rjwalters/loom/blob/main/mcp-loom/src/tools/sweeps.ts) — MCP tool definitions.
