# Sweep — Limitations and the daemon event bus

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** you need the deferred-vs-implemented feature status table, or the daemon event-bus wire contract (required whenever the in-process `loom-daemon` is running and the sweep child publishes phase events).
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Limitations (Deferred for Follow-up Issues)](#limitations-deferred-for-follow-up-issues)
- [Daemon event bus (Phase B of #3449 — #3453)](#daemon-event-bus-phase-b-of-3449--3453)
  - [When to publish](#when-to-publish)
  - [Topic taxonomy (frozen for v0.10.0)](#topic-taxonomy-frozen-for-v0100)
  - [How to publish — IPC contract](#how-to-publish--ipc-contract)
  - [Sample payloads for the six initial topics](#sample-payloads-for-the-six-initial-topics)
  - [Subscription (for tooling, not the sweep child)](#subscription-for-tooling-not-the-sweep-child)
  - [Failure modes (publisher side)](#failure-modes-publisher-side)
  - [Out-of-scope for Phase B](#out-of-scope-for-phase-b)

---

## Limitations (Deferred for Follow-up Issues)

The full `/loom:sweep` design in #3298 includes many features that are intentionally **not** part of this skill yet. Each of these is a candidate follow-up issue:

| Feature | Status | Notes |
|---------|--------|-------|
| Parallel waves (`--builders-per-wave N`) | **Implemented (#3316, auto default #3566, core-scaled #3693)** | Omitted flag resolves to an auto wave size at Stage -1 (#3566): up to 10 on the daemon detached-process path, core-scaled within `[3, 6]` (`clamp(floor((cores-2)/4), 3, 6)`, #3693) on the in-session subagent path. The `[3, 6]` band is **subagent-path-specific** (floor 3 is the #3289-safe validated minimum, ceiling 6 keeps a margin below single-account rate-limit burn and orchestrator context pressure — warns above only on explicit override `>= 7`); the daemon path scales to 10 because each sweep is an isolated process, not a nested subagent. This is a **width** knob — the #3289 "one level deep" nesting rule is unchanged: no nested `/loom:sweep` subagent. Issue-side only; ignored in Mode C. |
| Natural-language selectors (label/author/title/time-window filters via NL description) | **Implemented (#3318)** | Mode B in Arguments. Out-of-band queries (body/diff inspection, file-touch filters) still trigger clarification. |
| Build-everything sentinel (`/loom:sweep all`) | **Implemented (#3568; aggressive whole-backlog redefinition)** | Bare, sole `all` token (case-insensitive) resolves **every** open issue via `gh issue list --state open` (no label filter) and aggressively drives each toward a merged PR: curates uncurated/`loom:triage`/`loom:curating` issues, reclaims stale `loom:building` claims (one-time `recover-orphaned-shepherds.sh --recover` pass + `updatedAt` staleness) — since #6167 the same pass also reclaims stale PR-side `loom:reviewing`/`loom:treating` claim overlays on open PRs — probes `loom:blocked` for a cleared blocker, fans `loom:epic` out to its `loom:epic-phase` children, and routes existing open PRs to Judge/Doctor/Merge via the #3359 probe (which takes precedence). Only `loom:operator-only` and `loom:needs-capability` (#5817) are hard-skipped, except a capability-matched `loom:operator-mechanical` item, which routes to a propose-only (never live) lane (#6893). `all --prs` resolves every open PR (Mode C C0 filters non-actionable); a no-actionable-label advisory block (#6218) names any resolved PR C0 will skip, at the confirmation gate, before it silently drops out. Mandatory confirmation gate; `--dry-run` / `--builders-per-wave` / `--no-daemon` compose unchanged (recovery pass skipped under `--dry-run`). Multi-token `all …` phrases still route to Mode B/C. |
| `--dry-run` | **Implemented (#3319, extended in #3384)** | Prints the candidate plan (with wave grouping) and exits without mutating labels, worktrees, or PRs. Issue-set (Modes A/B) and PR-set (Mode C) output formats. |
| Existing-PR detection in pre-flight | **Implemented (#3359, #3677, phrase filter #6216)** | Pre-flight probes the union of `closedByPullRequestsReferences` (closing-keyword PRs) **and** timeline `cross-referenced` open-PR events that are phrase-confirmed against the PR body (non-closing `Part of #N` / `Contributes to #N` PRs only — a bare mention of `#N` in a PR body no longer counts, #6216); routes existing open linked PRs to Judge (or Merge if already `loom:pr`) instead of dispatching a duplicate Builder. Multi-PR ambiguity skips with a log. |
| `loom:operator-only` enforcement | **Implemented (#3360)** | Pre-flight skips issues with `loom:operator-only` (human action required: credentials, infra, hardware). Champion `--merge` mode also refuses to auto-promote them. |
| `loom:needs-capability` enforcement | **Implemented (#5817)** | Same hard-skip parity as `loom:operator-only` above, in both the Aggressive candidate taxonomy table and Mode C's C0 pre-flight — a narrower claim (missing tool/agent capability, not operator-by-right) that today skips identically. |
| Capability-aware `loom:operator-mechanical` lane | **Implemented (#6885 Part 2, #6893)** | The one carve-out from `loom:operator-only`'s hard skip, in the Aggressive candidate taxonomy table and Mode C's C0 pre-flight (Mode A/B conservative skips unchanged). Four fail-closed gates: `LOOM_WORKER_CAPABILITIES` set on this host (unset ⇒ inert, the default everywhere); labels are exactly `loom:operator-only` + `loom:operator-mechanical` and none of the other three sub-kinds / `loom:needs-capability` / `loom:blocked` / `loom:operator`; the body declares ≥1 recognized `<!-- loom:capability=<name> -->` marker (#6892, parsed by `./.loom/scripts/extract-capability-markers.sh` — never a hand-rolled regex); every declared capability is held (ANDed). **Propose mode only** — commands or a PR for an operator to approve, never a live credentialed action; a live mode needs its own explicit opt-in. A declared-but-unheld capability posts a **capability request** comment naming the gap instead of stalling silently. Any judgement call mid-task hard-stops and relabels to `loom:operator-decision`/`-objective`. Daemon-side parity: `loom-daemon/src/capability.rs` + `WorkItem::is_skipped_with_capabilities`. See "Capability-aware `loom:operator-mechanical` lane". |
| `loom:operator` merge-hold enforcement | **Implemented (#6398)** | Unlike the two hard-exclusion rows above, `loom:operator` (`.loom/docs/label-state-machine.md`) is **not** a candidate-wide skip — it stays in the normal re-evaluation queue. The gap it closes is narrower: before routing an already-`loom:pr`-labeled PR straight to `merge-pr.sh` (issue-side existing-PR probe's `1, has loom:pr label` row, and Mode C's C1c), check the same PR labels already read for `loom:operator` and skip the merge (not the whole candidate) if present — Champion's merge-risk hold stays in force until a human clears it. `verdict-staleness-guard.sh` (#5686) does not clear this hold either, so the two checks are additive, not redundant. |
| Operator-gate advisory scan (`all` sentinel body-text phrasing + title-prefix) | **Implemented (#5137, extended #5817, #6391)** | The `all` sentinel's aggressive candidate survey scans each candidate's already-fetched `body` (no new API call) for instruction-shaped operator-gate phrasing (`operator-gated`, `Operator decision:`, `login-walled`, `requires credentials`, …), for a declared `Depends on`/`Requires #A` dependency where `#A` carries `loom:operator-only` **or** `loom:needs-capability` (the `#87 → #4` shape), and independently reads each candidate's `title` (one extra `--json title` call) for a prefix match against `Operator:` / `Operator —` / `Operator-only` (#6391 — catches a title-declared operator-gated issue whose body has none of the phrase vocabulary). Matches ANNOTATE the confirmation-gate listing and `--dry-run` plan with a `⚠` suffix — advisory only, never a hard skip, never a label mutation, never blocking. Zero matches ⇒ byte-for-byte unchanged output. `./.loom/scripts/warn-operator-gated.sh`, covered by `defaults/scripts/tests/test-warn-operator-gated.sh`. **Does not catch decision-shaped or credential/verification-shaped acceptance criteria** — those are a semantic gap, not a missing phrase; the confirmation gate's operator judgement is the backstop (#6197, see "What this scan does NOT catch" under "Operator-gate advisory scan"). |
| Checkpoint/resume after kill | **Implemented (#3373)** | Per-issue phase checkpoint at `.loom/sweep-checkpoint/issue-<N>.json`. Sweep reads on entry and skips completed phases. No mid-builder recovery — kill during Builder resumes at builder start, worktree preserved by `worktree.sh` idempotency. Mode C reuses the helper keyed by the PR's closing-issue number (`closingIssuesReferences`); PRs without a `Closes #N` reference run without checkpointing. |
| PR-set mode (`--prs` flag and PR NL triggers; Judge/Doctor/Merge from current PR label) | **Implemented (#3384)** | Mode C. Skips Curator, Approval gate, Builder. Size-1 waves. `--builders-per-wave` ignored. Reuses issue-keyed checkpoint via `closingIssuesReferences`. |
| Daemon backend detection (Stage -1) | **Implemented (#3454, daemon-owned-child short-circuit #3829, `--claim-owned` flag #4111)** | Strict-AND between daemon reachability and multi-account pool. Mode C, `--no-daemon`, and a daemon-dispatched child (`LOOM_SWEEP_CLAIM_OWNED` set or `--claim-owned N` passed, #3829/#4111) short-circuit to subagent — the last **before** any probe, so a daemon child never re-probes/re-dispatches the daemon that spawned it (the circular-round-trip idle-hang fix). No implicit auto-start. Dispatch-only — Phase D does not subscribe to the event bus. See "Stage -1: Backend detection". |
| Concurrent-`/loom:sweep` run-state isolation + peer detection | **Implemented (#3768)** | A stable per-sweep-run id (`sweep-run-registry.sh new`) is generated once at sweep start and threaded through all `--task-id` checkpoint writes and the main-clean baseline path (`main-clean-baseline-${RUN_ID}.txt`), so two concurrent sweeps no longer clobber each other's baseline or share an ambiguous `sweep-$$` `task_id`. Stage 0b adds a loud, NON-BLOCKING peer-`/loom:sweep` warning via a dead-PID-pruned run registry (`.loom/sweep-run/`). Merge-target (default-branch) isolation is out of scope — that is #3759's stacking concern. See "Sweep Run Identity + Peer-`/loom:sweep` Detection". |
| Sweep-owned forge-claim lease renewal (Phase 1 of epic #6165) | **Implemented (#6180); start moved into dispatch code (#7672)** | A detached background loop idempotently PATCHes the `<!-- loom:lease host=... sweep=... -->` comment #6179's dispatch-time write leaves on `N` (never creates one), every ~5 minutes (configurable), for as long as the sweep's own long-lived process stays alive. **Who issues the one-shot `sweep-lease-renew.sh start` differs by path**: for a daemon-dispatched `--claim-owned` child, `SweepRegistry::finish_issue_dispatch` does it (`--watch-pid <child pid> --host <published host> --sweep-id <id>`) before the session's prompt even runs — Step 1a is mechanically guaranteed rather than prose-mandated, after one skipped instance cost 2.5 h of fleet thrash (#7672). Step 1a keeps a fallback `start` gated on the `LOOM_SWEEP_LEASE_RENEW_DISPATCHED=<N>` capability marker the same dispatch exports, since an installed prompt (refreshed by `git pull`) can run against a pre-#7672 binary (rebuilt only by `loom update`). For every in-session path (operator `/loom:sweep`, `--no-daemon`, GH Actions cron) Step 1b still starts it itself (#6320) — there is no dispatch code on those paths to do it. Either way renewal is **sweep-owned, never daemon-owned**: the loop is `disown`ed and watches the sweep's pid, so it self-terminates when the sweep exits (the lease ages out rather than being deleted) and a `loom-daemon` restart mid-sweep never interrupts it (#6129). See Step 1a / Step 1b in "1. Per-issue pre-flight". The daemon's own reclamation path reads it (Phase 2, #6286/#6287/#6288) and the sweep's own Builder phase fences on it before push/PR-open (Phase 3, #6309) — see the two rows below. |
| Daemon-side reclamation consults lease freshness (Phase 2 of epic #6165) | **Implemented (#6286/#6287/#6288)** | The daemon's periodic/startup reconciliation pass and the `recover-orphans` CLI both consult the freshest `loom:lease` comment's forge-assigned `updated_at` as the LAST gate before reclaiming a peer's `loom:building` claim (`loom-daemon/src/claim_reconciliation.rs` — `lease_is_fresh` / `fetch_freshest_lease_updated_at`, TTL = `LEASE_TTL_MINUTES_ENV`/`LOOM_LEASE_TTL_MINUTES`, default 15 min). `loom-daemon`-internal — see `defaults/docs/lease-record.md` § "Phase 2". |
| Sweep-side lease fencing before push/PR-open (Phase 3 of epic #6165) | **Implemented (#6309)** | The symmetric, sweep-owned counterpart to Phase 2 — fencing, not reclamation. Immediately before `git push` + opening the PR, the Builder phase runs `./.loom/scripts/sweep-lease-fence.sh check "$N"` (wired in `defaults/roles/builder-pr.md` § "Lease Fencing: Confirm You Still Own the Claim"), which reads the freshest `loom:lease` comment on `N` and confirms it is both fresh (same `LOOM_LEASE_TTL_MINUTES`/15-min-default TTL Phase 2 uses) and still owned by this sweep's own host. On failure (expired, exit `3`, or superseded by a different host's lease, exit `4`) the Builder aborts before pushing or opening a PR — it does not contest or clean up the peer's claim, the `loom:building` label is left alone. This bounds an acquisition-race overlap's cost to one wasted build rather than a duplicate reviewed/merged PR; it does not prevent the race itself (#4028 remains open). Runs unconditionally per-issue (fails open — exit `0` — when there is no lease to fence against, e.g. a manual `/loom:sweep`, GH Actions cron, or `--no-daemon` run), so this row has no Step-1a-style daemon-only gate unlike the two rows above. |
| In-session lease publication (writer-side hole in epic #6165) | **Implemented (#6320)** | Closes the gap where only `loom-daemon`'s dispatch ever wrote a lease record, leaving every in-session claim (operator `/loom:sweep`, `--no-daemon`, GH Actions cron) permanently reclaimable by any host. Per-issue pre-flight **Step 1b** runs `./.loom/scripts/sweep-lease-publish.sh publish "$N" --sweep-id "$RUN_ID"` for candidates Step 1a did not claim, then starts `sweep-lease-renew.sh` pinned to that exact `--host`/`--sweep-id`. Same record format as #6179, so #6286 (reclamation gate), #6287 (dispatch-time ordering) and #6309 (pre-push fence) all read it unchanged. Exit `4` (a peer host's fresh lease) is a pre-flight **skip**; exits `0`/`2` proceed. See Step 1b in "1. Per-issue pre-flight". |
| `--max-waves` cap | Deferred | Operator-level brake on long sweeps. |
| `--paused-merge` / `--no-judge` | Deferred | Merge-mode variants for trusted batches. |
| `--include-blocked` (unblock pass) | Deferred | Currently `/loom:sweep` skips `loom:blocked` issues outright. |
| `--curator-also` (parallel curators on `loom:triage`) | Deferred | Parallel triage is a separate orchestration question. |
| Config-driven defaults (`.loom/config.json` keys `sweep.*`) | **Partially implemented** | `sweep.escalation` (#3481, model ladder) and `sweep.max_doctor_cycles` (#3668, Doctor-cycle cap, default 1) are live and read at lifecycle-entry time. Other `sweep.*` knobs (e.g. `--max-waves` persistence) remain deferred. |
| Disk-pressure *gate* on auto wave size | **Implemented (#3566)** | Stage -1 resolves the auto wave size against free space on the **worktree-root filesystem** (via `loom_worktree_root`, so it measures the dedicated scratch volume when `LOOM_WORKTREE_ROOT` / `worktree.root` is set — #3539/#3541), clamping the target down and logging the reason. `LOOM_PER_WORKTREE_GB` (default 2) is the per-worktree estimate. |
| Disk-pressure *stop* condition (abort a running sweep on low disk) | Deferred | Only the initial auto wave size is gated (above); no mid-sweep abort. Wave sequencing limits disk usage; revisit if waves grow large. |
| Doctor-cycle counting across PRs | Deferred | The per-PR cap is now configurable (`sweep.max_doctor_cycles`, #3668, default 1) with a default-cap distinct-defect grace cycle, enforced inline. A *cross-PR aggregate* cycle budget (e.g. "at most K total Doctor cycles across a whole sweep") is still deferred. |
| Parallel Judges within a wave | Deferred | Sequential per-PR Judge today; needs benchmarking before parallelizing. Mode C is also strictly sequential per PR (size-1 waves). |
| Parallel PRs in Mode C | Deferred | Mode C uses size-1 waves. Multi-PR-per-wave is feasible (one judge per PR in parallel) but inherits the same #3289 risk that gated parallel issue-side Judges. |
| Mixed-mode invocations (some issues + some PRs in one `/loom:sweep`) | Won't fix (split into two calls) | Routing logic for the cross product of issue-state × PR-state is complex; cleaner to require two invocations. |
| Multi-closing-issue PRs (PR with `Closes #N` + `Closes #M`) | Partial — runs without checkpoint | Mode C logs all closing issues and proceeds with Judge/Doctor/Merge but skips checkpointing for the PR. Multi-key checkpoint variant is a follow-up. |
| PRs without `Closes #N` references | Partial — runs without checkpoint | Mode C logs a warning and processes the PR without checkpointing. Judge/Doctor/Merge are idempotent at the GitHub-state level so re-running on the next sweep is safe. |
| Cross-wave backfill on pre-flight skips | Won't fix | Intentionally clean wave boundaries — see step 1 of the Wave Lifecycle. |
| Intra-wave collision guard (overlapping PRs off a shared base) | **Implemented (#3647)** | Step 7 runs a read-only file-path overlap probe before each in-wave merge; overlapping PRs are updated onto the just-merged `main` and re-Judged (or Doctor→re-Judge on `DIRTY`) before merging, disjoint PRs keep the fast path. Step 8 adds a post-wave `buildGate.command`-against-`main` integration gate — the load-bearing backstop for cross-file semantic coupling (source-vs-test) that path-overlap cannot see; halts the sweep on a red `main`. Symbol/AST-level overlap detection is out of scope. |
| Partition-time (proactive) overlap awareness | **Implemented (#4161)** | Pre-flight parses each candidate's `## Affected Files` into an estimated file surface (missing/"To be determined" → unknown surface, excluded from analysis, never blocked); same-wave overlapping candidates are greedily reordered into different waves without breaking `--auto-stack` parent/child wave ordering, unavoidable overlap raises an explicit confirmation-gate warning naming the shared files + candidates, and `--dry-run` prints an `Overlap analysis` block. Complements the reactive #3647 step 7/8 gates (the fallback for overlap the partition couldn't avoid) so the Doctor-rebase cost is avoided rather than paid. **File overlap is a *scheduling* signal only — it never creates a stacking edge (#3729's rejection of file paths as a stacking-topology signal stays intact).** Cross-sweep coordination (#3768) and diff/AST-level surface inspection are out of scope. |
| Spinoff-issue filing for out-of-scope discoveries | Deferred | Only *orchestrator-side aggregation* of role-filed spinoffs into the Summary Output is deferred (build it once we have richer summary output to surface them cleanly) — role subagents (Builder/Judge/Doctor) already file their own follow-up issues via `./.loom/scripts/create-issue.sh` per their own role docs, and the sweep orchestrator must never suppress that behavior in a dispatch prompt. |
| Daemon `pipeline_state` situational awareness reads | Deferred | Skill only warns when the daemon is running. |
| Top-level vs namespaced naming (`/sweep` vs `/loom:sweep`) | **Resolved** | Ships as the namespaced `/loom:sweep` (and `/loom:loom` for the daemon operator), matching CLAUDE.md and `help.md`. Originally #3298 open question #1. |

For the full design discussion (including the open questions raised by the curator), see issue #3298.

## Daemon event bus (Phase B of #3449 — #3453)

When the in-process **loom-daemon** is running, the sweep child **must** publish phase-transition events onto the daemon's in-memory pub/sub bus so monitoring tools, the spawn loop, and any subscribed MCP layer can react in real time. This is the **wire-protocol contract** the skill exposes to the daemon (and via the daemon to the rest of Loom).

The bus is an in-process `tokio::sync::broadcast::channel<Event>` with a default capacity of **1024** events. It is **not** NATS/ZeroMQ — it lives only inside the running daemon and is gone the moment the daemon exits. Subscribers route by **topic prefix** (segment-aligned — `sweep.issue` matches `sweep.issue.123.phase` but not `sweep.issuetype.foo`). Slow subscribers receive a synthetic `topic_lag` event when they fall behind, then resume at the current channel head (pass-through, no silent drops; matches tokio's `Receiver::Lagged` semantics).

### When to publish

Publish a `sweep.issue.{N}.phase` event **immediately after the sweep skill commits a phase transition** — i.e. once the phase is durable in the forge (label flipped, comment posted, checkpoint written via `sweep-checkpoint.sh`). Do not publish before the side effects have landed; downstream subscribers treat the event as the authoritative signal that the phase is complete.

Publish a `sweep.issue.{N}.blocker` event when the skill chooses to mark the issue with a Loom-recognized blocker label (e.g., `loom:blocked`, `loom:operator-only`) and exits the lifecycle without proceeding to the next phase.

The daemon publishes `sweep.issue.{N}.exited`, `sweep.issue.{N}.crashed`, `sweep.global.dispatch`, and `sweep.global.completed` itself — the sweep child does **not** publish those.

### Topic taxonomy (frozen for v0.10.0)

The following six topics are the **entire** event vocabulary for v0.10.0. New topics require a follow-up issue — do not invent topics outside this table.

| Topic | Publisher | Payload (JSON) |
|-------|-----------|----------------|
| `sweep.issue.{N}.phase` | Sweep child via `PublishEvent` | `{"phase": "<phase-name>", "pr_number": <int or null>, "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.blocker` | Sweep child | `{"reason": "<short-text>", "label_added": "<label>", "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.exited` | Daemon reaper | `{"exit_code": <int or null>, "duration_sec": <int>, "repo": "<workspace-root>"?}` |
| `sweep.issue.{N}.crashed` | Daemon reaper | `{"checkpoint_phase": "<phase-name or null>", "repo": "<workspace-root>"?}` |
| `sweep.global.dispatch` | Daemon | `{"sweep_id": "<id>", "kind": {"type": "Issue", "value": <N>}}` |
| `sweep.global.completed` | Daemon | `{"sweep_id": "<id>", "outcome": "exited" | "crashed"}` |

`{N}` is the issue number (a positive integer). Phase names match the sweep-checkpoint schema (#3373): `curator`, `builder`, `judge`, `doctor`, `merge`, etc.

**`repo` field (optional, #3929)**: the four `sweep.issue.{N}.*` payloads carry an additive `repo` field naming the owning managed-workspace root, so a subscriber on the shared bus can disambiguate two managed repos that each dispatched a sweep for issue #N (the topic string is issue-scoped only). The **daemon stamps `repo` automatically** on the events it emits (`exited` / `crashed`). For the **child-published** `phase` / `blocker` events, include `repo` in the payload sourced from the `LOOM_WORKSPACE` env var the daemon exports to the sweep child at dispatch (e.g. `{"phase": "builder", "pr_number": 501, "repo": "$LOOM_WORKSPACE"}`). `repo` is optional and backward-compatible — omitting it is byte-for-byte the pre-#3929 behavior, and single-repo subscribers ignore it.

### How to publish — IPC contract

The daemon exposes a `Request::PublishEvent { topic, payload }` variant over its line-delimited JSON Unix-socket framing (the same socket used for `DispatchSweep`, `ListSweeps`, etc. — see `loom-daemon/src/ipc.rs`). One request → one `Response::EventPublished { topic, receivers }` ack frame.

**Sample wire frame** — sweep child advertises that it just finished the builder phase and opened PR #501:

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
```

The daemon responds with:

```json
{"type": "EventPublished", "payload": {"topic": "sweep.issue.123.phase", "receivers": 2}}
```

If no subscribers are listening, `receivers` is `0` and the event is dropped. **This is not an error condition** — the sweep child treats `receivers: 0` as "best-effort delivery, nobody home" and continues. Do not retry; the event is fire-and-forget.

### Sample payloads for the six initial topics

The following six samples are the authoritative reference for the payload schema of each frozen topic.

```json
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "curator", "pr_number": null}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "builder", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "judge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.phase", "payload": {"phase": "merge", "pr_number": 501}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.123.blocker", "payload": {"reason": "missing credentials", "label_added": "loom:operator-only"}}}
{"type": "PublishEvent", "payload": {"topic": "sweep.issue.456.blocker", "payload": {"reason": "dependent on #999", "label_added": "loom:blocked"}}}
```

The daemon-side events (these are **emitted by the daemon**, not by the sweep child — included here as the contract for subscribers):

```json
{"type": "EventStream", "payload": {"events": [{"type": "SweepExited", "issue": 123, "exit_code": 0, "duration_sec": 1842}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepCrashed", "issue": 456, "checkpoint_phase": "judge"}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalDispatch", "sweep_id": "sweep-issue-789-1717599600", "kind": {"type": "Issue", "value": 789}}]}}
{"type": "EventStream", "payload": {"events": [{"type": "SweepGlobalCompleted", "sweep_id": "sweep-issue-123-1717599600", "outcome": "exited"}]}}
```

### Subscription (for tooling, not the sweep child)

Long-running monitors subscribe with a single `Request::SubscribeEvents { topics }` frame and receive a stream of `Response::EventStream { events }` frames on the same open connection. Topic matching is prefix-aligned: `["sweep.issue.123"]` matches every event for issue 123; `["sweep.global"]` matches the two global topics; `[]` (empty list) matches everything on the bus.

```json
{"type": "SubscribeEvents", "payload": {"topics": ["sweep.issue.123", "sweep.global.completed"]}}
```

The sweep child itself does **not** subscribe — it only publishes. Subscription is consumed by the operator-facing monitoring tools that shipped in Phase C (#3455) and any custom MCP-bridged tool an operator wires up.

### Failure modes (publisher side)

- **Daemon not running**: the Unix-socket connect fails. The sweep child must treat this as a soft error and continue without publishing — Loom is designed to run without the daemon. Log a single `debug` line and proceed.
- **Daemon running but no subscribers**: `Response::EventPublished { receivers: 0 }`. Fire-and-forget; continue.
- **Bus capacity exhausted on the subscriber side**: the slow subscriber sees a `topic_lag` event; **the publisher is unaffected** and never blocks. The bus is bounded but tokio's broadcast channel has pass-through overflow on the receiver, not the sender.

### Out-of-scope for Phase B

These live in the daemon, not the sweep skill — do **not** implement them in the sweep skill. The operator-facing MCP tools shipped in Phase C (#3455); the rest are frozen non-goals:

- Operator-facing MCP tools (`get_sweep_status`, `subscribe_to_events`, `tail_event_bus`) — daemon-side, shipped in Phase C (#3455).
- New topics beyond the six listed — frozen for v0.10.0 per epic #3449; file a follow-up issue if you "need" one.
- Distributed bus / cross-daemon coordination — explicit non-goal (single broker, in-process).
- Persistent event log or replay — explicit non-goal (transient bus).
- Consumer groups / durable subscriptions — explicit non-goal.

