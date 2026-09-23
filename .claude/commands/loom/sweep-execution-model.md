# Sweep — Execution model — dispatch, parallelism, model selection

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** **always, before dispatching any subagent** (all modes). Contains the three CRITICAL dispatch invariants, the model-selection/escalation ladder, the Doctor-cycle cap, experiment mode, and the cached-read wrapper contract.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Execution Model](#execution-model)
  - [CRITICAL: Only Builders parallelize — issue-creating roles must be serialized (issue #3707)](#critical-only-builders-parallelize--issue-creating-roles-must-be-serialized-issue-3707)
  - [CRITICAL: Subagent dispatch is async-only — you MUST block explicitly (issue #3822)](#critical-subagent-dispatch-is-async-only--you-must-block-explicitly-issue-3822)
  - [CRITICAL: One level deep — never spawn a nested orchestrator (`/loom:sweep`) as a subagent](#critical-one-level-deep--never-spawn-a-nested-orchestrator-loomsweep-as-a-subagent)
  - [Model selection for subagent dispatch (issue #3477, Phase 1)](#model-selection-for-subagent-dispatch-issue-3477-phase-1)
  - [Model escalation on Judge rejection (issue #3481, Phase 2)](#model-escalation-on-judge-rejection-issue-3481-phase-2)
  - [Effort-aware rung grammar, the `fable` rung, and refusal fallback (issue #3702)](#effort-aware-rung-grammar-the-fable-rung-and-refusal-fallback-issue-3702)
  - [Credit-exhaustion fallback — one rung down, any rung (issue #5687)](#credit-exhaustion-fallback--one-rung-down-any-rung-issue-5687)
  - [Spend-limit fallback — session-default first, any rung (issue #6518)](#spend-limit-fallback--session-default-first-any-rung-issue-6518)
  - [Why no pre-resolved chain (issue #5697)](#why-no-pre-resolved-chain-issue-5697)
  - [Doctor-cycle cap (`sweep.max_doctor_cycles`, issue #3668)](#doctor-cycle-cap-sweepmax_doctor_cycles-issue-3668)
  - [Model-cost experiment mode (`sweep.modelExperiment` / `LOOM_MODEL_EXPERIMENT`, issue #3725)](#model-cost-experiment-mode-sweepmodelexperiment--loom_model_experiment-issue-3725)
  - [Other constraints](#other-constraints)
  - [Cached forge reads (`gh-cached`, #4667)](#cached-forge-reads-gh-cached-4667)

---

## Execution Model

`/loom:sweep` processes the candidate list in **waves**:

- **Mode A/B (issue-set)**: the candidate list is partitioned into waves of up to `N = --builders-per-wave` issues, where an omitted flag resolves to the Stage -1 auto wave size (see "Resolve auto wave size" — up to 10 on the daemon path, core-scaled within `[3, 6]` on the subagent path, disk-clamped). Issues are picked into waves in order. Within a wave, builders are dispatched in parallel; across waves, processing is sequential. Each wave fully settles (all builders → per-PR Judge → optional Doctor → merge) before the next wave starts.
- **Mode C (PR-set)**: the candidate list is processed in **size-1 waves** (one PR per wave). `--builders-per-wave` is ignored because there is no Builder phase. Each PR is routed per its current label (Judge / Doctor→Judge / Merge — see "PR-set Wave Lifecycle" below) and fully settles before the next PR is touched. Sequential per-PR processing is a **width** choice — parallel Judge/Doctor across PRs is unbenchmarked and every wave member's Task result is read back into this orchestrator session (context pressure) — and parallels the issue-side "per-PR Judge is sequential within a wave" policy. It is **not** the #3289 rule, which governs nested (grandchild) dispatch depth, not wave width.

> **CRITICAL — GitHub `mergeable`/`mergeStateStatus` is a base-branch-only check, NOT a sibling-PR conflict check.** GitHub evaluates every PR against the **base branch** independently; it has **no concept of the other PRs a sweep has in flight in the same wave**. So two sibling PRs that both edit the same file can each report `MERGEABLE` / `CLEAN` at the same instant, and the conflict only becomes visible **after the first one merges** — the second PR flips to `CONFLICTING` the moment its base moves. This repo's branch ruleset provides **no** server-side backstop either: it has no `required_status_checks` and no "require branches up to date before merging" rule, so `merge-pr.sh --auto` will merge a clean-but-stale sibling immediately without re-checking it against the new `main`. **Never treat a green `mergeable`/`mergeStateStatus` as evidence that a wave's PRs are mutually compatible.** Two defenses cover this gap: the **proactive** overlap-aware partitioning below (keep same-file candidates out of the same wave, or warn) and the **reactive** intra-wave overlap revalidation in Wave Lifecycle step 7 (re-check each about-to-merge PR against the just-merged `main`). See "Overlap-aware wave partitioning" and step 7.

### CRITICAL: Only Builders parallelize — issue-creating roles must be serialized (issue #3707)

**Waves parallelize Builders only.** The reason a wave can safely fan out `N` agents at once is that each Builder works in an isolated git worktree and produces **exactly one PR at the end** — no shared mutable forge state is touched mid-run, so two concurrent Builders never collide. `/loom:sweep` itself only ever dispatches Builders (plus per-issue Curator/Judge/Doctor, which run **sequentially within a wave**), so today's wave loop is safe by construction.

**Exception: the git stash stack (#4821).** `refs/stash` is shared across
*every* linked worktree of the repo, not per-worktree — so if two
concurrent Builders in a wave both use bare `git stash` for ad-hoc WIP
handling, one can pop or drop the other's entry (observed in production:
kicad-tools PRs #4524/#4526). This is the one piece of shared mutable state
the isolated-worktree argument above does not cover. Builders must use
`./.loom/scripts/worktree.sh snapshot <issue-number>` (a per-worktree patch
file) instead of `git stash` for WIP — see `defaults/roles/builder.md`.

**Never dispatch two or more issue-creating agents concurrently.** Agents that **create issues** — Architect proposals, Curator oversized-issue decomposition, Champion epic-phase creation — mutate the forge's **shared, server-assigned issue-number space** with no client-side coordination, transaction, or idempotency key. When two such agents run `gh issue create` bursts at the same time they **race on issue numbers and cross-contaminate bodies** (one epic's title paired with another's body), and any recovery/retry loop that PATCHes-by-title amplifies the damage by winning every write race against the other still-active filer. This is not hypothetical: it was observed 2026-07-21 on a 4-wide wave (1 builder + 3 architects) — 2 duplicate issues, 3 with mismatched title/body, and a corrupted roadmap comment, all needing manual reconciliation (#3707).

This rule targets roles whose **primary output is an issue-creation burst** — Architect, Curator-decomposition, Champion epic-phase — not any single `create-issue.sh` call anywhere in a wave: a Builder, Judge, or Doctor filing one follow-up issue for an out-of-scope discovery it hit mid-task is not the hazard this section guards against and remains permitted, and expected, even inside a parallel wave.

Concrete rules for anyone extending this skill or hand-driving a wave:

- **Do NOT construct a mixed wave** that places any issue-creating role (Architect / Curator-decomposition / Champion epic-phase) alongside Builders — or alongside another issue-creating agent. That exact `1 builder + 3 architects` shape is the footgun this section forbids.
- **Serialize issue-creating agents**: one must finish its entire `gh issue create` burst before the next starts. A recovery/retry loop must never run against a still-active concurrent filer. **"Serialize" here means awaited-to-completion, not merely dispatched-with-a-sync-flag** — see "Subagent dispatch is async-only" below (#3822).
- Parallel **Builders** remain safe and are the only role `/loom:sweep` fans out — this is unchanged.

Heavier mitigations (a per-wave issue-filing lock, an epic-scoped idempotency UUID + post-create reconciliation, or a serialized issue-filing sub-phase inside `/loom:sweep`) are **deferred, out-of-scope follow-ups** to this documentation guardrail — build them only if serialization-by-convention proves insufficient in practice (#3707).

### CRITICAL: Subagent dispatch is async-only — you MUST block explicitly (issue #3822)

**The harness may launch every Agent/Task subagent asynchronously regardless of the dispatch flags.** In particular, `run_in_background: false` is **not** a guarantee of synchronous return — it has been observed ignored, with the agent launched async anyway (2026-07-23, Claude Code harness). An orchestrator that trusts a sync-flag and proceeds immediately can start a downstream serialized phase before the upstream agent has finished — e.g. begin Judge before builders finish, or overlap two issue-creating agents (the exact #3707 race this skill forbids).

Therefore, at **every** dispatch site where this skill sequences one phase after another, the orchestrator **MUST explicitly await each subagent's completion** — via the context-safe recipe below (issue #6168: a single big blocking `TaskOutput` call is the wrong tool for this) — before advancing. Do not rely on any dispatch flag to enforce ordering. Concretely, this makes the skill's sequencing rules load-bearing on an explicit await, not on the harness:

- **Sequential Curator per issue** (step 2) — await each Curator before the next.
- **"Await all builders before Judge"** (step 4) — collect every builder's `TaskOutput` before any Judge dispatch.
- **Sequential per-PR Judge / Doctor within a wave** (steps 5–6) — await each PR's Judge (and its Doctor→Judge cycle) before the next PR's Judge.

**"Serialized" therefore means awaited-to-completion, not merely dispatched-with-a-sync-flag.** The #3707 rule above depends on this: serializing issue-creating agents is only safe if each is explicitly awaited to completion before the next is dispatched — a `run_in_background: false` that the harness ignores would silently overlap them.

**Sharper hazard in headless `claude -p` mode (issue #4257): ending your turn IS the kill signal.** Everything above frames the async-dispatch hazard as an *ordering* bug — the next phase starting too early. In headless `-p` mode there is a second, more severe consequence: **ending the orchestrator's turn terminates the `claude -p` process, and that process exit kills every still-running background child, full stop.** There is no "it'll finish in the background after I'm done talking" — once the orchestrator writes its final message and the turn ends, the process (and therefore every subagent it spawned) is gone. Concretely:

- **Never dispatch a role subagent (Curator / Builder / Judge / Doctor) with `run_in_background: true`** in a sweep. There is no safe way to "fire and forget" a role dispatch here — a headless sweep has no later turn in which to check on it.
- Because `run_in_background: false` is **not** honored as a synchronous-return guarantee either (see above), the only safe pattern in either case is: **write the orchestrator's final message only after every dispatched subagent's completion has been explicitly observed** — via the context-safe recipe below, one recipe for each. If you have not yet observed completion for a dispatched subagent, you MUST NOT end the turn.
- **The await itself must be context-safe, not a blind blocking `TaskOutput` (issue #6168).** A blocking `TaskOutput` on a still-running `local_agent` Task/Agent subagent is the wrong tool by the harness's own documentation, which warns against reading a background task's raw `.output` file — the full subagent conversation transcript (JSONL) — because it "will overflow your context window". That is exactly what has been observed live: a `TaskOutput(block=true, timeout=600000)` call on a still-running Builder subagent returned a multi-kilobyte raw JSONL transcript dump into the orchestrator's context on timeout, instead of a small status result. The context-safe recipe differs by session mode, and this skill cannot always tell interactive from headless apart (the `guard-background-subagents.sh` Stop hook itself now can, as of #6645 — it blocks only in headless mode and downgrades to a one-line advisory in an interactive session, so ending an interactive turn is no longer blocked):
  - **Interactive session** (a human driving Claude Code, or any invocation that is not `claude -p`): background agents keep running across turns even after the current turn ends, so just end the turn and let the harness's completion notification arrive on a later turn. Do not call a blocking `TaskOutput` here — it buys nothing over the notification and risks the JSONL-dump hazard above.
  - **Headless `claude -p`** (this skill's normal mode — no later turn exists, because ending the turn kills the process, see below): await in-turn with a **bounded, non-blocking poll loop** instead — call `TaskOutput` with `block: false` (or a short `timeout`), read only the result's `<status>` tag (`running` → sleep and poll again in the same turn; `completed`/`failed` → resolved), and never call `TaskOutput` with `block: true` and a long timeout. This is the same bounded in-turn poll-loop discipline already required below for backoff waits and long-running Bash operations.
- **Failure signature to match in forensics**: a sweep log whose final line is something like *"…in the background. I'll wait…"* immediately followed by process exit — the orchestrator believed a background task would keep running unsupervised, then ended its turn, killing it. This exact incident: `sweep-issue-4195.log`, PR #4243, where the backgrounded Judge was killed mid-review and left a stale `loom:reviewing` claim on the PR.
- **Never end a turn while a monitored background task — not just a subagent — is the only pending work** (issue #4366). This is the same kill signal, just triggered by a `Bash run_in_background` task, a `Monitor` wait, or any other "I'll check back on this later" narration instead of a role-subagent dispatch. A long-running operation (a cache/dependency download, a build, a CI wait) MUST be awaited **in-turn** via a bounded poll loop (repeatedly check status, sleeping between checks, inside the SAME turn) — never parked on a monitor and left for "a future turn" to pick back up, because in headless `claude -p` mode that future turn never arrives: the process has already exited.
- **Second failure signature to match in forensics** (issue #4366, observed 2026-07-28): a sweep log whose final line narrates something like *"Cache download is running in the background (monitored). I'll pick this back up once it completes or the fallback check fires."* immediately followed by a clean process exit (exit code 0) — indistinguishable from a legitimate self-skip by exit code alone, but with **zero lifecycle progress**: no checkpoint written, no PR opened, no phase advanced. The daemon reaper's no-progress backstop (`SweepExited.no_progress`) now catches and quarantines this shape after repeated occurrences, but the skill-level fix is to never produce it in the first place — poll in-turn instead of parking on the monitor.
- **A transport failure (529/Overloaded, connection reset, network error) is the SAME hazard, not an exception to it** (issue #4462, observed 2026-07-29). When a dispatched subagent (Curator / Builder / Judge / Doctor) dies to a transport error, the ONLY two safe responses in headless `-p` mode are: **(a) retry the dispatch inline, in the SAME turn** (re-invoke the subagent, optionally after a short in-turn `sleep`+poll if you want to space retries), or **(b) once inline retries are exhausted, exit NONZERO** so `claude-wrapper.sh` / the daemon retry machinery re-runs the sweep from its last checkpoint. **NEVER arm an end-of-turn backoff** — a `Monitor {command: "sleep 90 && …"}` / `ScheduleWakeup` wait followed by "I'll retry when the timer fires" narration and a turn end. In `-p` mode that "future turn" never arrives: the process exits at turn end, so the timer has no session to wake, and — because the exit code is **0** — the wrapper logs "completed successfully", the reaper sees a clean exit, and the issue is stranded in `loom:building` with no PR and no live sweep. **Backoff means a bounded in-turn sleep-and-retry loop, never an armed timer you end your turn on.** Third failure signature to match in forensics: a sweep log whose final lines are *"Backoff timer armed (90s). I'll retry the Builder dispatch when it fires."* immediately followed by a clean exit-0 — the exact #4462 incident (`sweep-issue-4426-1785358105`, two 529 kills then an armed `Monitor` backoff, ~35 min orphaned).
- **This prose guardrail is not the only line of defense.** A mechanical `Stop`-hook backstop (`defaults/hooks/guard-background-subagents.sh`, issue #4257, coverage extended to background Bash tasks by #4389 and to armed `Monitor`/`ScheduleWakeup` waits by #4462) blocks the turn from ending — once, per stop sequence — when it detects an unresolved dispatched Task subagent, an outstanding `run_in_background` Bash task, or an armed-but-unfired `Monitor`/`ScheduleWakeup` timer in the transcript. See `defaults/docs/guard-hooks.md`'s "Background Subagent Stop Guard" section for how it works and how to verify it is wired in a given repo.

### CRITICAL: One level deep — never spawn a nested orchestrator (`/loom:sweep`) as a subagent

`/loom:sweep` dispatches `loom-builder`, `loom-judge`, and `loom-doctor` subagents **directly from this orchestrator session** in a single tool-call block. This is **one level deep** and is empirically safe for `N` up to at least 3.

**Do NOT, under any circumstances, dispatch a nested orchestrator skill (`/loom:sweep`) as a subagent from `/loom:sweep`.** That would be two levels deep (parent Claude → `/loom:sweep` Task → builder/judge Task) and triggers the nested-dispatch stall hazard tracked in #3289 (stream-pump dies on parallel grandchildren). The wave loop in this skill is the architectural answer to that race — preserve it.

Concretely, when this skill says "dispatch builders for the wave", that means: in a single tool-call block, invoke `loom-builder` once per issue in the wave (e.g., three parallel `Task` calls if `N=3`). It does **not** mean invoke `/loom:sweep` three times.

If a future maintainer is tempted to "simplify" by replacing the wave-loop with parallel `/loom:sweep` calls: don't. Read #3289, then read this section again.

### Model selection for subagent dispatch (issue #3477, Phase 1)

Every role subagent dispatched by this skill (`loom-curator`, `loom-builder`, `loom-judge`, `loom-doctor`) gets its model resolved through a fixed precedence chain. Resolve once per role at dispatch time and pass the result via the Task tool's `model` parameter:

1. **Explicit dispatch param** — a model explicitly requested by the operator for this sweep (e.g., an operator instruction in the invoking prompt).
2. **Workspace override** — `.loom/config.json` → the `terminals[]` entry whose `roleConfig.roleFile` matches the role (e.g., `builder.md`) → its optional `roleConfig.model` field.
3. **Role default** — `.loom/roles/<role>.json` → `suggestedModel` (ships as an alias: `sonnet`, `opus`, or `haiku`).
4. **Session default** — if none of the above resolves (or resolves to an empty string), **omit the `model` parameter entirely** so the subagent inherits the parent session's model. Never pass `model: ""`.

**Logical-tier resolution before dispatch (issue #3982).** Every rung, tier, and arm in this skill names a *logical tier* by its CLI alias (`sonnet`, `opus`, `fable`) — but the bare `opus` alias still resolves to a **previous-generation** model on the wire (`claude-opus-4-8`), while `sonnet`/`fable` resolve to the current generation. So the shipped ladder `sonnet → sonnet@xhigh → opus → fable` would silently step *down* a generation at the `opus` rung. To fix this **without** scattering a pinned ID across the skill, resolve the chosen model through the single indirection point **immediately before** you pass it to the Task tool's `model` parameter (or to a spawned child's `--model`):

```bash
RESOLVED_MODEL="$(./.loom/scripts/resolve-model.sh "$LOGICAL_MODEL")"   # e.g. opus -> claude-opus-5
```

- Apply this to **every** resolved model on the dispatch path: the escalation-ladder rung, the No-Fable-Judge `opus` fallback, the `fable → opus` refusal fallback, and the experiment's Arm A (`assign-arm --resolve` does the same resolution for you — see "Model-cost experiment mode"). The Tier 2.5 complexity-tier resolution passes through `resolve-model.sh` **inside** `resolve-tier-model.sh`, so its output is already a concrete ID.
- `resolve-model.sh` maps only the stale logical tiers to concrete IDs (today `opus → claude-opus-5`); it **passes unknown aliases and pinned IDs (`claude-sonnet-4-6`) through unchanged**, and preserves the `@effort` suffix. So resolving is always safe — a tier-1/tier-2 operator pin, a `sonnet` rung, or a `sonnet@xhigh` rung all survive untouched.
- The mapping is configurable in `.loom/config.json` → `sweep.modelAliases` (an additive tier → ID object), so an operator can repoint a tier — or drop the pin once the CLI's own `opus` alias rolls to gen-5 — with no code change. Absent block ⇒ shipped default.
- The pinned ladder strings in this document (`sonnet → sonnet@xhigh → opus → fable`) stay written in **logical aliases** on purpose — the resolution happens at dispatch time, not in this prose.

**Pinned-ID degradation on Task-tool dispatch (issue #4282).** The resolution above yields a **concrete model ID** (`claude-opus-5`), but *which dispatch surface* carries it decides whether that ID is passable — exactly like the `@effort` degradation at "Effort passthrough vs. graceful degradation" below, and it composes with it:

- **Process-spawn / daemon path — the pinned ID IS passed through.** A spawned `/loom:sweep` child (`mcp__loom__dispatch_sweep`, or a direct `spawn-claude.sh --model <id>`) reaches the `claude` CLI, which accepts a pinned ID. #3982's guarantee holds unchanged here.
- **In-session Task tool — the pinned ID DEGRADES to its family alias.** This skill dispatches its per-role subagents (Curator/Builder/Judge/Doctor) through the **Task tool**, one level deep (see "CRITICAL: One level deep"), whose `model` parameter is an **alias-only enum** (`sonnet | opus | haiku | fable`) — a pinned ID like `claude-opus-5` is an invalid value there. So on this path, degrade the resolved ID back to its nearest Task-passable alias with `resolve-model.sh --task-alias` **immediately before** you pass it to the Task tool's `model` parameter, and if it changed, emit a **loud log line** noting the substitution and its generation cost:

  ```bash
  TASK_MODEL="$(./.loom/scripts/resolve-model.sh "$RESOLVED_MODEL" --task-alias)"   # claude-opus-5 -> opus
  ```

  e.g. `model resolution: pinned ID 'claude-opus-5' not passable on Task-tool dispatch — degraded to alias 'opus' (gen 5 → gen 4)`. `--task-alias` strips any `@effort` suffix too (the Task tool has no effort knob — same as the #3705 rule). **Exit 3** ⇒ no Task-passable alias (a non-Claude runtime ID, an unparseable value) ⇒ **omit the `model` parameter** so the subagent inherits the parent/agent-definition model; never dispatch a guessed alias, and never block a sweep on resolution.
- **Retirement (AC-style, like the `@effort` rule):** this degradation exists only because the CLI/harness `opus` alias still lags a generation. When it rolls to gen 5, drop the pin via `.loom/config.json` → `sweep.modelAliases: {"opus": "opus"}`: `resolve-model.sh` then returns the bare `opus` alias, `--task-alias` is the identity, and the degradation disappears with **no code or prose change**.

**Tier 2.5 — complexity marker (issues #3702, #4238, Builder dispatch only)**: between tier 2 and tier 3, at **Builder** dispatch, resolve the Builder's model from the Curator-emitted `<!-- loom:complexity=<tier> -->` marker (an HTML comment; values `mechanical` | `routine` | `complex`, absent ⇒ `routine`; see `curator.md`). The marker classifies the issue on one axis — *would a mistake be caught?* — into three cost-of-being-wrong strata, and the model for that stratum is resolved from `sweep.tierModels[<runtime>][<tier>]` (a runtime-neutral map of logical tiers), falling back to the active **`sweep.optimization` profile preset** (`cost` | `speed` | `balanced`, see below) when `tierModels` has no entry for that runtime/tier. **Resolve by command, not by judgement** — do not read a model out of the config yourself:

```bash
MODEL="$(./.loom/scripts/resolve-tier-model.sh <issue> <runtime>)"   # e.g. mechanical -> haiku, complex -> opus
```

- **Exit 0** ⇒ `$MODEL` is the resolved concrete ID (already passed through `resolve-model.sh`); pass it to the Task tool's `model` parameter (or export `LOOM_MODEL` / pass `--model "$MODEL"` to a spawned child). This **replaces** the tier-3 `suggestedModel` resolution for the Builder. On the Task-tool path this concrete ID degrades via `resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch" above.
- **Exit 3** ⇒ neither `sweep.tierModels` nor the optimization preset has an entry for the runtime/tier (the default — no such block ships in `defaults/config.json`, and the default `balanced` profile's preset is empty); **fall through to the tier-3 role default unchanged.** An unconfigured repo (or one with `sweep.optimization` unset/`"balanced"`) therefore dispatches **byte-for-byte identically to today**. Existing curated issues (which carry no marker) are unaffected.

**`sweep.optimization` — cost/speed policy switch (issue #4238 Phase B).** An operator-facing profile in `.loom/config.json` → `sweep.optimization`: `"cost"` | `"speed"` | `"balanced"` (default `"balanced"`), with env override `LOOM_SWEEP_OPTIMIZATION` (precedence **env > config > default**, the standard pattern used by `sweep.escalation` / `sweep.max_doctor_cycles`). It selects a **preset** over the `sweep.tierModels` map above rather than a fixed bump — see `resolve-tier-model.sh` / `resolve_optimization_profile` / `optimization_preset` in `loom-daemon/src/script_helpers/model_tiers.rs` for the implementation, and `defaults/docs/model-selection.md` for the full preset table. An explicit `sweep.tierModels[<runtime>][<tier>]` entry, if the operator has set one, still wins over the preset — the preset only fills tiers `tierModels` leaves unmapped. An invalid `sweep.optimization` value warns and falls back to `balanced`; it never fails dispatch.

Hard bounds, all enforced here (apply identically to both `sweep.tierModels` and the `sweep.optimization` preset — the profile is just an alternate source for the same tier-2.5 resolution, not a separate mechanism with separate rules):

> **Experiment-mode suppression (issue #3725).** When `sweep.modelExperiment` resolves to `experiment` (see "Model-cost experiment mode" below), the forced arm **overrides and SUPPRESSES this tier-2.5 resolution** for the Builder: the marker is still *read* (same grep) and used **only as the stratification key**, never as a model override (the experiment strata `complex` vs. the rest, so `mechanical` collapses with `routine` there). This is load-bearing — without it, a `complex`-marked issue on Arm B (sonnet-first) would silently jump models and confound the A/B. The tier map (and the `sweep.optimization` preset behind it) applies normally whenever the experiment is `off`/`observe`.

- **Never resolves to `fable`.** `resolve-tier-model.sh` refuses a tier map or optimization preset that names (or resolves to) `fable` and falls through instead. Fable is reached only via the escalation ladder (objective Judge-rejection evidence) or an explicit operator param, never on a Curator's speculation or an operator's cost/speed profile.
- **It is not a label** and creates no label — it lives only in the issue body.
- **Tier-1 and tier-2 pins still win.** The marker (and the optimization profile behind it) sits *strictly between* tiers 2 and 3: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) overrides it, exactly as they override tier 3.
- The marker applies **only to the Builder path**. It never influences Curator, Judge, or Doctor resolution. (The fork's cost-of-being-wrong design additionally raises the Judge to match a `complex` Builder; that Judge-side change is deferred to a follow-up so this change stays byte-identical when the tier map is unconfigured.)
- Log the resolved model and reason per dispatch, e.g. `model: builder=haiku (complexity=mechanical)`.

**No-Fable-Judge hard invariant (issue #3702)**: **Judge model resolution can never resolve to `fable`, regardless of `sweep.escalation` contents or any marker.** The escalation ladder and the tier-2.5 marker apply only to the Curator-marker→Builder path and to the rejection-triggered Doctor — never to Judge. The Judge is the escalation sensor (see #3481); reviewing security-adjacent diffs is precisely Fable's refusal surface, and a refusing Judge would deadlock the control loop. If a resolved Judge model would ever be `fable` (alias or pinned ID), fall back to `opus` for the Judge dispatch and log the substitution. **Ordering matters on the Task-tool path**: apply this No-Fable fallback **first** (fall to `opus`), *then* run the resolved model through the Task-tool degradation (`resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch") — `task_alias_of` maps a fable-family ID to `fable` mechanically, so aliasing before the No-Fable fallback would defeat the invariant.

Rules:

- Aliases (`sonnet`/`opus`/`haiku`) and pinned IDs (`claude-sonnet-4-6`) are both valid at every tier. Shipped role JSONs use aliases; workspaces that need determinism pin exact IDs in `roleConfig.model`.
- A retry of the same role for the same issue (e.g., Builder re-dispatch after a mid-builder kill, or a second Judge pass after Doctor) **reuses the same resolved model**. Transport-level retries inside `claude-wrapper.sh` (token exhaustion, crashes, 5xx) likewise always keep the model — they are not quality signals and never trigger escalation.
- **Exception — Judge-rejection escalation (issue #3481, Phase 2)**: a Doctor dispatched *because of* a `loom:changes-requested` transition escalates one rung up the capability ladder. See "Model escalation on Judge rejection" below.
- Resolution failures are soft: if a role JSON is missing or unparseable, fall through to the next tier silently. Model selection must never block a sweep.
- The daemon path has its own equivalent: `mcp__loom__dispatch_sweep` accepts an optional `model` param which the daemon forwards to the spawned child as `claude --model <value>`. When delegating to the daemon (Stage -1 `use_daemon`), you MAY pass a resolved model; when omitted, the child inherits the spawning environment's default — the daemon emits no `--model` flag at all.

### Model escalation on Judge rejection (issue #3481, Phase 2)

When the Judge requests changes and this orchestrator dispatches a Doctor for the rejected PR — the Doctor phase at issue-side step 6 and at Mode C step C1b — the Doctor's model escalates one rung up a capability ladder instead of resolving through tiers 3/4 of the precedence chain.

**The ladder** lives in `.loom/config.json` under `sweep.escalation`:

```json
{
  "sweep": {
    "escalation": ["sonnet", "opus"]
  }
}
```

Three states:

| `sweep.escalation` value | Behavior |
|--------------------------|----------|
| Key absent | Default ladder `["sonnet", "opus"]` applies |
| `[]` or `false` | Escalation disabled — pure Phase 1 behavior; the rejection-triggered Doctor resolves through the unmodified precedence chain |
| Non-empty array | As configured; rungs accept aliases or pinned IDs, same as every other tier |

Rules:

1. **Trigger**: escalation fires **only** on a real Judge rejection — the `loom:changes-requested` transition that routes into the Doctor phase. First attempts of every role (Curator, Builder, the first Judge pass) always use the unmodified Phase 1 precedence chain. `ladder[0]` never overrides anything — it documents what attempt 1 is *expected* to run on, it is not applied.
2. **Precedence interaction**: the rejection-triggered Doctor resolves to `ladder[1]`, but only when its model would otherwise come from tier 3 (role `suggestedModel`) or tier 4 (session default). Tier 1 (explicit dispatch param) and tier 2 (`roleConfig.model` workspace pin) still win — pins are pins; operators who pinned want determinism.
3. **Composes with the cap, does not extend it**: escalation composes with the configurable Doctor→Judge cycle cap (`sweep.max_doctor_cycles`, default 1 — see "Doctor-cycle cap" below); it never raises the cap on its own. Consume the ladder generically as `ladder[min(attempt - 1, len - 1)]`: cycle 1 (attempt 2) resolves `ladder[1]`, cycle 2 (attempt 3) resolves `ladder[2]`, and so on. When the cap is at its default of 1, only `ladder[1]` is reached on the normal path (a configured third rung stays dormant); raising `max_doctor_cycles` above 1 — or granting the default-cap distinct-defect grace cycle — activates deeper rungs automatically, with no change here.
4. **Mode C inherits the rule** — C1b runs the identical Doctor phase under the identical cap, so the identical `ladder[1]` rule applies. No separate policy.
5. **Resume safety**: the escalation decision derives from the `loom:changes-requested` label/phase, **not** from a stored counter — so a sweep killed between Doctor dispatch and the follow-up Judge resumes correctly: re-entry routes back through the Doctor/Judge phases per the checkpoint skip rules, and any re-dispatched rejection-triggered Doctor escalates again. The optional `attempt` field on the sweep checkpoint (`sweep-checkpoint.sh write N doctor-done ... --attempt 2`) is forward-compat bookkeeping for a future cap raise; readers treat an absent field as attempt 1.
6. **The orchestrator decides, never the wrapper**: escalation is resolved here at Doctor-dispatch time. `claude-wrapper.sh` / `spawn-claude.sh` retries always keep their model (transport failures are not quality signals), and no wrapper change is involved.

### Effort-aware rung grammar, the `fable` rung, and refusal fallback (issue #3702)

This subsection extends the `sweep.escalation` ladder above with an optional richer rung grammar and a top `fable` rung. It is **fully additive and opt-in**: the shipped default ladder stays `["sonnet", "opus"]` with `max_doctor_cycles` `1`, and bare-alias configs parse and behave byte-for-byte as documented above. Nothing here changes default behaviour.

**Rung grammar — `model@effort`.** Each rung in `sweep.escalation` is either:

- a **bare alias** (or pinned ID) — `"opus"` resolves to `(model=opus, no effort override)`, exactly as today; or
- an **`alias@effort`** form — `"sonnet@xhigh"` resolves to `(model=sonnet, effort=xhigh)`. The part before `@` is the model (alias or pinned ID); the part after `@` is the effort level passed through to the dispatched role.

Escalating the cheaper dimension first (`sonnet → sonnet@xhigh → opus → fable`) retries at ~Sonnet cost before committing to Opus's higher output pricing. A rung with no `@` never carries an effort override, so existing arrays are unaffected.

**Effort passthrough vs. graceful degradation (issue #3705).** Whether an `alias@effort` rung's effort half actually reaches the dispatched role depends on **which dispatch surface** carries it — the two surfaces differ, and only one exposes a per-call effort knob:

- **`claude` CLI / process-spawn / daemon path — effort IS passed through.** The `claude` CLI exposes `--effort <level>`, and `spawn-claude.sh` threads it via a `LOOM_EFFORT` env → `--effort` passthrough (mirroring the `LOOM_MODEL` → `--model` plumbing; #3705). This is reachable whenever a whole `/loom:sweep` child is spawned as an OS process (`mcp__loom__dispatch_sweep` / a direct `spawn-claude.sh` invocation). It sets a **session-default** effort for that child, and `spawn-claude.sh` logs a structured `spawn-claude: effort=<level>` line for greppable per-run observability (model-parity, #3482). Note this is session-wide for the child, **not** per-rung.
- **In-session Task tool — effort DEGRADES to the bare model.** The sweep dispatches its per-role subagents (Builder/Judge/Doctor) through the **Task tool**, one level deep (see "CRITICAL: One level deep"), and the Task tool exposes **no** effort / reasoning-effort parameter alongside `model`. Because the escalation ladder's per-rung `@effort` is consumed at that per-role dispatch time, a resolved `alias@effort` rung on this path **resolves to the bare model** (the `@effort` suffix is dropped) and the orchestrator emits a **loud log line** noting the degradation, e.g. `escalation: effort plumbing unavailable on Task-tool dispatch — rung 'sonnet@xhigh' degraded to bare model 'sonnet'`. Never treat a malformed or empty effort (`sonnet@`, `sonnet@@x`) as an error — it falls back to bare-model dispatch with the same loud line; model resolution must never block a sweep.

The grammar ships either way so configs stay stable across environments: a `sonnet@xhigh` rung raises effort wherever the CLI/process path carries it, and degrades cleanly to bare `sonnet` on the Task-tool path — no config edit needed in either case, and if the Task tool later gains an effort parameter the same config activates the per-rung bump automatically.

**The `fable` rung.** `fable` (alias, or a pinned frontier-model ID where alias resolution is unavailable at a given tier — do not hard-code a specific ID into shipped defaults) is a valid **top** rung. Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]` under the Doctor-cycle cap, a `fable` rung placed at index ≥ 3 is only ever reached when `max_doctor_cycles ≥ 3` — i.e. it is **opt-in** and never appears on the shipped default ladder.

**Recommended opt-in deep-ladder recipe** (`.loom/config.json`) — pairs a 4-rung ladder with the cap raise required to reach its deeper rungs (see "Doctor-cycle cap" below):

```json
{
  "sweep": {
    "escalation": ["sonnet", "sonnet@xhigh", "opus", "fable"],
    "max_doctor_cycles": 3
  }
}
```

**Refusal-aware fallback for the `fable` rung.** Fable-class safety classifiers refuse some legitimate security-adjacent work (guard hooks, OAuth token handling, credential scanning) with `stop_reason: "refusal"` — which `classify_error` (`.loom/scripts/lib/classify-error.sh`) reports as `MODEL_REFUSAL`. On a `MODEL_REFUSAL` at a `fable` rung, the orchestrator **re-dispatches the same attempt one rung down** (`fable → opus`) **without consuming a Doctor cycle**. A refusal is a *routing error*, not a quality signal, so it must not eat the escalation / `max_doctor_cycles` budget: the `attempt` counter is unchanged, and the retried Doctor is still the same cycle `k`. This is distinct from a Judge rejection (which advances the attempt and escalates *up*). Only the `fable` rung has a rung below it to fall to; a `MODEL_REFUSAL` at a non-`fable` rung is handled by the normal error path.

**No-Fable-Judge invariant (restated).** Judge dispatch never resolves to `fable`, regardless of ladder contents — see the invariant under "Model selection for subagent dispatch". The ladder here governs only the rejection-triggered Doctor.

### Credit-exhaustion fallback — one rung down, any rung (issue #5687)

**The signature.** `You're out of usage credits` — which `classify_error` (`.loom/scripts/lib/classify-error.sh`) reports as **`MODEL_CREDITS_EXHAUSTED`**, a category of its own. Credits are scoped to a **model tier**, so this is *not* the account dying: the same account, on a cheaper model, is still fully usable. Observed 2026-08-08 on a wave-width-6 `/loom:sweep all` run — all six wave builders were dispatched on the session-default model and all six died within minutes of each other when that tier's credits ran out.

**Why this is not `TOKEN_EXPIRED` / `TOKEN_EXHAUSTED`.** Those name the *account credential* dying (weekly/session/plan limit, expired OAuth token); the remedy is rotating to a different token in the pool, and on those signatures it is the only remedy. Credit exhaustion is one axis narrower, and the difference is decisive **on the in-session path specifically**: subagents dispatched through the Task tool share the orchestrator's own credential and have **no token pool to rotate through** — but their `model` parameter is chosen fresh at every dispatch. The remedy that does not exist here (rotate the account) and the one that does (drop a model rung) are exactly inverted relative to the wrapper/daemon path, which is why the two classes must not be conflated. (A forge GraphQL rate limit is a *third* axis — neither of these; see "Mid-phase-death recovery".)

**The response — same attempt, one rung down, no Doctor cycle consumed.** Structurally identical to the `MODEL_REFUSAL` handling above, keyed on a different signature and **not restricted to the `fable` rung**:

1. Resolve the cheaper rung **by command, not by judgement**:

   ```bash
   NEXT_MODEL="$(./.loom/scripts/resolve-model.sh "$CURRENT_MODEL" --downgrade)"   # opus -> sonnet
   ```

   `--downgrade` steps one rung down the Task-tool cost ladder `fable → opus → sonnet → haiku`, accepts a bare alias, a pinned ID, or an `@effort` form, and always emits a **Task-passable alias** — so it subsumes the `--task-alias` degradation and no second pass is needed. Its `fable → opus` hop is deliberately the same hop the refusal fallback above hard-codes. **Exit 3** ⇒ no cheaper rung; see terminal behavior below.
2. **Re-dispatch the same phase for the same issue at `$NEXT_MODEL`** — same attempt, same worktree, same claim, same wave. For a Builder this is exactly the "resume from builder start" path the Builder scope limit already prescribes (`worktree.sh` is idempotent, and the resumed builder decides for itself whether to commit, amend, or discard the partial diff its predecessor left).
3. **Do not consume a Doctor cycle and do not advance the `attempt` counter.** Credit exhaustion is an *availability* fault, not a quality signal: it must not eat the `max_doctor_cycles` budget, and it must never trigger the *upward* escalation ladder, which is reserved for real Judge rejections.
4. **Log the substitution loudly**, matching the refusal-fallback / Task-degradation log conventions:
   `credit exhaustion: issue #N builder killed (MODEL_CREDITS_EXHAUSTED) — re-dispatching same attempt at 'sonnet' (was 'opus'); no Doctor cycle consumed`

**Recover the whole wave, not one subagent.** Credit exhaustion is **correlated across a wave by construction** — every builder in a wave shares one account and, absent per-issue tier-2.5 overrides, one model. N simultaneous deaths is the *expected* observation, not a coincidence. So:

- Re-dispatch **every** affected member of the wave at its downgraded model **in a single tool-call block**, exactly as the wave was originally dispatched. Do not serialize the recovery — that turns one wave-width-6 outage into six sequential retries.
- Downgrade **per subagent, from that subagent's own resolved model**. A tier-2.5 `complex` builder on `opus` and a `mechanical` builder on `haiku` are not on the same rung; never pick one replacement model for the whole wave.
- Never re-dispatch a phase that already wrote a checkpoint, and never re-dispatch a member that exited cleanly.

**Terminal behavior — when there is no rung below.** If `resolve-model.sh --downgrade` exits 3 (the phase was already at `haiku`, or the model is not Claude-family), **stop downgrading**: there is no cheaper tier, and retrying the same one dies the same way. Fall through to the "Mid-phase-death recovery" procedure below — re-verify forge state, complete only the missing steps — and if the phase still cannot be completed, record the issue as `rate-limited (unresumable: <phase> MODEL_CREDITS_EXHAUSTED mid-phase, no cheaper model rung available)` and advance. **One rung per kill, never a blind loop**: if the re-dispatch at the cheaper rung dies with the same signature, downgrade once more *from that rung*. The ladder terminates at `haiku`; never re-dispatch the same phase at the same model on this signature.

**What this does not change.**

- **The No-Fable-Judge invariant holds trivially** — the ladder only descends, so a downgrade can never land on `fable`.
- **The daemon / process-spawn path rotates, it does not downgrade.** With no per-dispatch model knob, `claude-wrapper.sh` answers `MODEL_CREDITS_EXHAUSTED` by rotating the account. #8058 changed the mark's *scope*, not the remedy: it names the model class in flight, and `tokens select --model` skips only that class, so an Opus ceiling no longer starves Sonnet. Class-less marks still block the account; `loom-daemon health`/`status` break the healthy count down per class. See `defaults/docs/token-pool.md`.

### Spend-limit fallback — session-default first, any rung (issue #6518)

**The signature.** `"You've hit your monthly spend limit"` / `"hit your ... spend limit"` — the exact wording observed 2026-08-19 killing an in-session `model: opus` Builder mid-turn ("Agent terminated early due to an API error: You've hit your monthly spend limit · raise it at claude.ai/settings/usage"). `classify_error` (`.loom/scripts/lib/classify-error.sh`) already reports this as **`TOKEN_EXHAUSTED`** (added by issue #5631) — that classification is correct and unchanged for the **subprocess / daemon path**: `claude-wrapper.sh`'s `is_account_exhaustion` already rotates to a different pooled account on it, same as any other `TOKEN_EXHAUSTED`. Nothing in this section touches that path.

The gap is the same one #5687 (`MODEL_CREDITS_EXHAUSTED`) closed for credit exhaustion: **on the in-session Task-tool dispatch path there is no subprocess, so `classify_error` never runs at all.** The orchestrator must pattern-match the raw Task-tool failure text itself, exactly as it already does for the credit-exhaustion signature. This document names that signature `SPEND_LIMIT` purely as **this orchestrator's own reason-prefix label** for the phrase — it is not, and does not need to be, a `classify_error()` return value; the actual classifier still reports `TOKEN_EXHAUSTED` for this text everywhere it runs.

**Why the remedy differs from the credit-exhaustion fallback above.** `MODEL_CREDITS_EXHAUSTED` is *definitionally* scoped to one model tier — "You're out of usage credits" always means the same account is fine on a cheaper model, so walking `resolve-model.sh --downgrade` is always a safe first move. A monthly **spend** limit is not definitionally scoped that way: it reads like a dollar cap on the whole account, which a cheaper model would also eventually hit, not a per-tier ceiling. The 2026-08-19 incident is consistent with *either* reading (the killed dispatch carried an explicit `model: opus` override; a bare re-dispatch with no `model` param — inheriting the session default — succeeded), so **do not assume the per-tier reading and jump straight to a cost-ladder walk.** Apply the remedy the incident actually validated, and let the account-wide case reveal itself instead of being guessed at:

1. **If the killed dispatch carried an explicit `model` param** (a role `suggestedModel`, a `sweep.escalation` rung, or any other override above session default): **re-dispatch the exact same attempt with the `model` param OMITTED entirely** — not a rung-down alias, the parameter itself absent, so the subagent inherits the parent session's own model. This is the literal remedy the incident observed working, and it makes no assumption about whether the limit is per-tier or account-wide.
2. **If that session-default re-dispatch also dies with the same `"spend limit"` signature — or the killed dispatch had no explicit `model` param to omit in the first place (it was already running at session default)** — this is decisive evidence of the **account-wide** reading: no substitution of models help here, because there is no `model` left to change out from under it. **Stop substituting models.** Fall straight through to the ordinary "Mid-phase-death recovery" procedure below (re-verify forge state, complete only the missing steps), and if the phase still cannot be completed, record the issue as `rate-limited (unresumable: <phase> SPEND_LIMIT mid-phase, no session-default fallback available)` and advance.
3. **Same attempt, no Doctor cycle consumed, at any rung** — identical to the credit-exhaustion and refusal fallbacks: the `attempt` counter is unchanged, and this never triggers the *upward* escalation ladder (reserved for real Judge rejections). Applies whether the killed dispatch was a first-attempt role resolution or a `sweep.escalation`-ladder rung — a rejection-triggered Doctor escalated onto a spend-limited rung gets the same omit-the-model-param treatment before its cycle is considered consumed.
4. **Log the substitution loudly**, matching the existing conventions: `spend limit: issue #N builder killed (SPEND_LIMIT) — re-dispatching same attempt with model param omitted (was 'opus'); no Doctor cycle consumed`.

**Recover the whole wave, not one subagent.** Like credit exhaustion, a spend-limit kill can plausibly correlate across a wave (shared account); apply the same "re-dispatch every affected member in one tool-call block, never serialize" discipline described under "Credit-exhaustion fallback" above.

**What this does not change.**

- **The No-Fable-Judge invariant holds trivially** — omitting a `model` param can never resolve to `fable`.
- **The daemon / process-spawn path keeps its own remedy**, same as `MODEL_CREDITS_EXHAUSTED` — `claude-wrapper.sh` already treats this signature as `TOKEN_EXHAUSTED` (rotate the account, mark it exhausted) via the existing #5631 pattern. One daemon-side **attribution** bug did have to be fixed alongside this section: the reaper's own crash-signature table (`loom-daemon/src/sweep_registry/crash_signals.rs::exhaustion_signatures`) still carried the pre-#5631 fixed alternation (`hit your (limit|session limit|weekly limit)`), so a spend-capped detached child matched **no** signature there and the reaper charged the **issue's** insta-crash quarantine tally instead of marking the account — the same backwards attribution #4501 fixed for the per-model ceiling. That table is now back in lockstep with the shell classifier's bounded-filler shape. The account pool's *treatment* is unchanged either way (`rate-limited`, transient exhaustion).
- **No new `classify-error.sh` category.** Unlike `MODEL_REFUSAL`/`MODEL_CREDITS_EXHAUSTED`, this section does not need one: the subprocess path already classifies the phrase correctly as `TOKEN_EXHAUSTED`, and the in-session orchestrator keys off the raw phrase directly (same mechanism `MODEL_CREDITS_EXHAUSTED`'s own text match uses upstream of any classifier call). A distinct daemon-side category remains a legitimate, separate follow-up if crash-forensics telemetry ever wants to distinguish a spend cap from other `TOKEN_EXHAUSTED` causes — out of scope here.

### Why no pre-resolved chain (issue #5697)

Issue #5697 asked, before any code was written, whether the reactive one-rung-down remedy above should instead pre-resolve a *whole* fallback chain at dispatch time. **Closed as not worth building** — no `sweep.fallbackChain` config key, no dispatch-time chain data structure — for three reasons, each answering one of the issue's own questions:

1. **A "chain" is not an independent fact to configure — it is fully derivable.** `fable → opus → sonnet → haiku` is exactly what repeatedly applying `model_tiers::downgrade_task_alias` (`resolve-model.sh --downgrade`) produces, in order, every time. There is no second source of truth a `sweep.fallbackChain` key could usefully override without drifting out of sync with the ladder `downgrade_task_alias` already hard-codes — a config surface that only exists to duplicate a pure function is a liability, not a feature.
2. **The Task tool dispatch takes exactly one `model` param per call, and the orchestrator must re-dispatch on every kill regardless.** Pre-resolving rungs 2, 3, and 4 up front buys nothing when only rung 2 can ever be used on the *next* dispatch — the orchestrator has no way to hand the subagent a fallback list it could consult on its own mid-turn. Whether the rung was resolved a step early (at wave-dispatch time) or reactively (at kill time, which is what happens today) it is looked up exactly once per re-dispatch either way.
3. **`downgrade_task_alias` is already an O(1) deterministic table lookup**, not an expensive computation — there is no recomputation being saved by resolving it ahead of the kill instead of at the moment it is needed.
4. **The daemon/process-spawn path does not want this at all.** It has no per-dispatch model knob to pre-load a chain into (see "What this does not change" above) — it responds to `MODEL_CREDITS_EXHAUSTED` by rotating pool accounts, never by downgrading models, so a pre-resolved chain would have no consumer on that path. (#8058 scoped that mark to one model class, not a substitution.)

**Item 2 (daemon-path telemetry) — implemented, not closed.** Unlike the chain question above, tagging credit exhaustion distinctly in the daemon's per-sweep outcome telemetry *was* worth building: the gap was real (a `MODEL_CREDITS_EXHAUSTED` daemon-dispatched death was previously indistinguishable from a plan/quota `TOKEN_EXHAUSTED` death in the durable outcomes journal) and the fix is a pure reporting addition. The reaper's crash classifier (`sweep_registry::classify_account_exhaustion` / `classify_crash`) now recognizes the `MODEL_CREDITS_EXHAUSTED` prose ("You're out of usage credits") as its own `model-credits-exhausted` signature, distinct from `rate-limited` / `rate-limit-abort` / `model-limit`, and that classification is persisted on the durable `#4644` outcomes journal's `crash_classification` field (`account-exhausted:model-credits-exhausted`). The account pool's health policy is unchanged — a reporting-only distinction. (`tokens_pool::health`'s `TokenExhausted | ModelCreditsExhausted` arms were fused until #8058 Phase 2; a class-naming mark now holds just it, in `class_cooldowns`.)

### Doctor-cycle cap (`sweep.max_doctor_cycles`, issue #3668)

The Doctor→Judge cycle cap bounds how many times a single PR can bounce between Judge and Doctor before it is blocked for human attention. It exists to stop Judge/Doctor disagreement loops and bound worst-case latency. The cap is configurable in `.loom/config.json` under `sweep.max_doctor_cycles`, read once at lifecycle-entry time the same way `sweep.escalation` is:

```json
{
  "sweep": {
    "max_doctor_cycles": 1
  }
}
```

Three states:

| `sweep.max_doctor_cycles` value | Behavior |
|---------------------------------|----------|
| Key absent | Default cap of **1** applies — one Doctor→Judge cycle per PR (the historical behavior) |
| Invalid (non-integer, or `< 1`) | Falls back to the default cap of **1** and logs a warning; a malformed config never blocks a sweep |
| Valid integer `>= 1` | Up to that many Doctor→Judge cycles per PR before the PR is blocked |

**Counting.** A "cycle" is one Doctor pass plus the re-Judge that evaluates it. The cap reuses the existing `attempt` checkpoint field: attempt 1 is the Builder's PR (or the PR as it enters Mode C); the Doctor dispatched after the first Judge rejection is attempt 2 (cycle 1), the Doctor after the second rejection is attempt 3 (cycle 2), and so on. Doctor cycle `k` is permitted while `k <= max_doctor_cycles` (equivalently `attempt <= max_doctor_cycles + 1`). When the cap is reached and Judge still requests changes, block the PR — add `loom:blocked`, leaving the Judge's `loom:changes-requested` in place (that label pair is what Champion's recovery pass below keys on) — log `PR #P blocked: doctor cycle exhausted after <k> Doctor→Judge round(s); human attention required`, and advance to the next candidate. The `attempt` value written on each Doctor cycle is `k + 1`; the checkpoint schema already accepts any positive integer, so no plumbing change is needed to reach attempt 3+.

**Escalation composes.** Because the ladder is consumed as `ladder[min(attempt - 1, len - 1)]`, raising the cap activates deeper rungs automatically (see "Model escalation on Judge rejection" point 3). The cap and the ladder are independent knobs.

**Distinct-defect exception (default cap only).** When `max_doctor_cycles` is at its **default of 1** and the *second* Judge rejection names a defect that is demonstrably **distinct** from the first rejection's defect — forward progress (the first fix worked and uncovered a genuinely new problem), not thrash (the same disagreement re-litigated) — the orchestrator MAY grant **exactly one** additional bounded Doctor→Judge cycle before blocking. This is a judgment call made by comparing the two Judge rejection comments:

- **Distinct defect** (e.g. rejection 1 = "duplicate ampacity rules"; rejection 2 = "root-only test-permission flaw uncovered after the dedup fix") → grant one grace cycle, and **emit a required log line** naming the distinction, matching the block-log convention so the grant is auditable:
  `PR #P: granted one extra Doctor cycle — second rejection is a distinct defect (<short reason>)`.
- **Same defect re-rejected, or ambiguous** → **block immediately** per the cap. The anti-thrash guarantee is unchanged for the thrash case.

Constraints that keep the exception from becoming an unbounded loop:

- It is **single-use per PR** — one grace cycle only. A *third* rejection after the grace cycle always blocks, even if it too looks distinct.
- It applies **only at the default cap** (`max_doctor_cycles == 1`). When an operator has already raised the cap above 1, the exception does **not** compose on top — the configured cap is the entire budget. (Layering a per-rejection grace cycle onto an operator-raised cap would reintroduce the indefinite-thrash risk the cap exists to prevent.)
- The distinction MUST be stated in the log line. An unlogged grace cycle is a bug.

**Champion-side counterpart (issue #4574).** Once a PR is blocked, Champion's Capped-PR Recovery Pass (`champion-pr-merge.md` → "Capped-PR Recovery Pass") reconsiders it — open PRs carrying `loom:blocked` + `loom:changes-requested` — and may grant a further bounded Doctor→Judge cycle by removing `loom:blocked`. It applies **this same forward-progress test**, just at a different decision point: periodically, post-mortem, with the PR's complete rejection history instead of the dying sweep's local context. The two do not compose into a double-grant (a PR reaches `loom:blocked` only after the in-sweep exception was consumed or was not applicable) and **neither imposes a numeric cap on the other** — the in-sweep exception stays single-use per PR, and Champion's repeat grants are bounded by re-applying the forward-progress test each round, not by a shared counter.

### Model-cost experiment mode (`sweep.modelExperiment` / `LOOM_MODEL_EXPERIMENT`, issue #3725)

> **Fallback note (issue #4809).** For a **daemon-dispatched** sweep (the
> normal `dispatch_sweep` / work-finder / epic-supervisor path), arm
> assignment and the forced Builder model are now resolved **natively in the
> daemon at dispatch time** (`sweep_registry::resolve_autonomous_dispatch_model`)
> — the daemon computes the SAME deterministic arm this section describes and
> passes the resolved model as the dispatch `--model`, so it wins the #4501
> default-pin precedence instead of being silently overridden by it. Per-sweep
> outcome telemetry (`sweep.outcome` records) is likewise attributed an `arm`
> automatically from the dispatched model, with no action from this skill.
> This is the fix for two compounding defects discovered in the 2026-07-31
> canary rollout: (1) the deterministic per-phase instructions below are
> **LLM-authored prose** and were observed to never execute in a headless
> `-p` child — no banner, no `assign-arm`/`record` calls, zero rows in
> `.loom/stats/sweep-model-stats.jsonl` across a full day of canary sweeps;
> and (2) even when they *would* execute, the #4501 dispatch model pin is
> tier-1 and structurally outranks a model only ever "forced" in prose. The
> instructions below remain a **best-effort fallback** for non-daemon
> contexts (a manual `/loom:sweep` run in an interactive session, or a
> Task-tool in-session dispatch that never passes through `dispatch_sweep`) —
> keep following them there — but they are no longer the primary data path
> for a daemon-dispatched canary.

This mode instruments a sweep to produce the balanced A/B evidence #3718 needs to decide the Builder `opus → sonnet` retune. **It is off by default and is byte-for-byte a no-op when unset** — every deterministic instruction below runs only when the mode resolves to `observe` or `experiment`. All the arithmetic (mode resolution, arm assignment, the durable append, the harvest) lives in `./.loom/scripts/sweep-experiment.sh` (a thin stub over `loom-daemon sweep-experiment`); this skill never computes a modulo by hand.

**Tri-state resolution (read once at lifecycle entry, same point as `sweep.escalation`).** Resolve `./.loom/scripts/sweep-experiment.sh resolve-mode` → one of `off` | `observe` | `experiment`. Precedence follows the **string-valued** guard pattern (`guards.rmScope` / `guards.forceScope`), not the boolean one:

- highest: `LOOM_MODEL_EXPERIMENT` env (`off`/`observe`/`experiment`) → then `.loom/config.json` → `sweep.modelExperiment` → default `off`.
- Unknown/malformed value → treated as `off` with a stderr warning; a bad value **never** aborts the sweep.

The three states:

| Mode | Behavior |
|------|----------|
| `off` | No instrumentation. Zero behavior change. No `.loom/stats/` file is created. |
| `observe` | Passive measurement. No model forcing, no arm. One JSONL record appended per phase (`arm` null). Safe to run anywhere. |
| `experiment` | Active A/B. Builder is forced to the assigned arm's model; records are tagged with the `arm`. **Canary-only** (see Guardrails). |

**Two arms map onto #3718's inequality.** `resolve-mode` in `experiment` picks a per-issue arm via `./.loom/scripts/sweep-experiment.sh assign-arm --issue N --complexity <routine|complex>` → prints `<arm> <model>`:

- **Arm A = opus-first** — Builder forced to `opus`; the normal escalation ladder still applies on Judge rejection. Resolve the printed `<model>` through `./.loom/scripts/resolve-model.sh` (or pass `--resolve` to `assign-arm`, which prints the already-resolved ID) before dispatch, so Arm A reaches **Opus 5** (`claude-opus-5`) on the wire rather than the stale gen-4 `opus` alias (issue #3982). Arm B's `sonnet` is unaffected (it passes through unchanged). **On in-session Task-tool dispatch** the pinned `claude-opus-5` is not passable and degrades to the `opus` alias via `resolve-model.sh --task-alias` — see "Pinned-ID degradation on Task-tool dispatch" (issue #4282); only the process-spawn/daemon path reaches Opus 5 on the wire.
- **Arm B = sonnet-first + escalate** — Builder forced to `sonnet`; on Judge rejection the Doctor escalates via the existing `sweep.escalation` ladder (#3481), exactly as documented in "Model escalation on Judge rejection". Arm B *is* the candidate policy #3718 is evaluating.

**Deterministic, resume-safe, stratified assignment.** The arm is a pure function of the issue number and the #3702 complexity stratum, so a killed-and-resumed sweep re-running the same issue **lands on the same arm**. The complexity marker is read once (the same grep at the tier-2.5 site) and serves two purposes: the **stratification key** (so both arms see a comparable difficulty mix) and — **only when the experiment is off/observe** — the tier-2.5 tier-map resolution. In `experiment` mode that resolution is suppressed (see the "Experiment-mode suppression" note under tier 2.5).

**Forced-arm precedence.** The forced arm slots into the Builder model-resolution chain **above tier 2.5 / tier 3** but **below tier 1 / tier 2 operator pins**: an explicit dispatch param (tier 1) or a `roleConfig.model` workspace pin (tier 2) still wins — a pinned canary is intentionally opted out of the experiment. The forced arm only ever replaces what tier 2.5 / tier 3 would have resolved for the Builder.

**Durable stats store.** Instrumentation appends one JSONL record per role phase invocation to `.loom/stats/sweep-model-stats.jsonl` (gitignored; survives the merge that deletes the transient checkpoint). Immediately after each phase's `sweep-checkpoint.sh write`, also run:

```bash
./.loom/scripts/sweep-experiment.sh record --mode <mode> --issue N --phase <curator|builder|judge|doctor|merge> \
  --role <role> --model <resolved-model> --arm <A|B|"" > --attempt <k> --complexity <routine|complex> \
  --verdict <pass|changes|""> --agent-id <agent-id> --stats-file .loom/stats/sweep-model-stats.jsonl
```

Each record carries the **HARD deterministic outcome-chain** (`arm`, `model`, `attempt`, `judge_verdict`, `cycle_count`, `complexity`) — which alone answers #3718's inequality (first-attempt Judge-pass rate + mean Doctor cycles × model price) — **plus the `agent-id` join key** for the role invocation (available in the Task-result metadata at dispatch/return time), which the harvest joins against #3726's transcript index to attribute exact cost.

**Token fidelity.** Live per-phase token capture is **not** available at the Task-result boundary; the exact input/output + cache split is recovered at **harvest** time by parsing each role subagent's `agent-<id>.jsonl` `usage` blocks (see below). Each record stamps a `token_fidelity` tag naming the source (`none` | `sweep-aggregate-log` | `transcript`). The deterministic outcome-chain is the load-bearing signal; exact cost just makes it precise.

**Guardrails (load-bearing).** `off` by default; `observe` is safe anywhere. `experiment` is **canary-only**: `resolve-mode` refuses to honor it on a non-canary target and **loudly downgrades to `observe`** unless the operator confirms a canary via an **uncommitted** signal — the `LOOM_MODEL_EXPERIMENT_CANARY=1` env var or the gitignored `.loom/CANARY` sentinel file. The committed `sweep.modelExperimentCanary` config flag is **no longer** an accepted confirmation (#3731): it would propagate with a copied config and fire experiment on production. A git-tracked `.loom/CANARY` is refused for the same reason. The `sweep.modelExperiment` *mode* may still live in committed config — it stays inert without the uncommitted confirmation. At lifecycle entry, print the loud banner naming the active mode, the canary confirmation source, and — in `experiment` — the arm assigned to the issue:

```bash
./.loom/scripts/sweep-experiment.sh banner --issue N --complexity <routine|complex>
```

**Harvest (exact per-role cost).** After a canary run, aggregate the store into the per-arm inequality inputs #3718 consumes — first-attempt Judge-pass rate, mean Doctor cycles, exact cache-aware cost per arm, and the merge-rate quality floor — via the reader alongside `agent-metrics.sh`:

```bash
./.loom/scripts/agent-metrics.sh --model-experiment --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
# equivalently: ./.loom/scripts/sweep-experiment.sh harvest --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
```

The harvest parses each joined `agent-<id>.jsonl` transcript's `usage` blocks (input/output + `cache_read_input_tokens`/`cache_creation_input_tokens`) and prices them with the same **cache-aware** per-model table as `loom-daemon`'s `resource_usage.rs`. Transcripts are located through #3726's `loom.transcript-index/v1` archive index (`--archive-dir` = `LOOM_TRANSCRIPT_ARCHIVE`); harvest should run periodically (cron) over a multi-day canary so usage is extracted into the compact stats store before `~/.claude/projects` is pruned.

> **Daemon detached-child path (honest finding, verified against on-disk transcripts).** The role-subagent transcripts of a daemon-dispatched `claude -p "/loom:sweep N"` child land under that child's own `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/<child-session-uuid>/subagents/agent-<id>.jsonl` tree — the **durable** location, not the ephemeral `/tmp/.../tasks/` scratch — and each carries the full per-message `usage` (input/output + cache split) and `model`. Confirmed present on disk for real detached-child sessions. So they are archivable/harvestable via the same #3726 periodic sync. What the daemon reaper does **not** yet know is the child's session-uuid, so it cannot trigger a precise single-session archive on exit — the cron periodic sync is the backstop, exactly as for the completion hook (see "Session Transcript Archival").

### Other constraints

- **Do NOT write to `.loom/daemon-state.json`.** That file is owned by the standalone daemon. `/loom:sweep` runs independently and must not race with the daemon on shepherd-slot bookkeeping. Reading `daemon-state.json` for situational awareness is fine; writing is not.

### Cached forge reads (`gh-cached`, #4667)

Every concurrent sweep, Judge, and Champion on this host shares **one**
personal `gh` rate-limit budget (#4665), and they re-issue the same candidate
listings and candidate surveys independently. Route those repeated
**observation** reads through the short-TTL cache wrapper; keep every
**arbitration** and **merge-gating** read on plain `gh`.

Resolve the wrapper **once**, at sweep start (alongside Step 0a's run id):

```bash
# Degrades to plain `gh` when the wrapper is absent or its Python runtime is
# broken — the same probe merge-pr.sh uses. Nothing in this document depends on
# the cache existing; it is a budget optimization, never a correctness mechanism.
GH_READ="gh"
_ghc="$(git rev-parse --show-toplevel 2>/dev/null)/.loom/scripts/gh-cached"
if [[ -x "$_ghc" ]] && "$_ghc" --version >/dev/null 2>&1; then GH_READ="$_ghc"; fi
```

**Route through `$GH_READ` (cached, 30s TTL):**

| Call site | Where |
|---|---|
| Mode B / Mode C candidate resolution (`gh issue list` / `gh pr list` translations) | Validation rules, Examples |
| The `all` sentinel's whole-backlog `gh issue list --state open --limit 100` | Validation rules |
| The one-per-invocation `gh label list --limit 200` token validation | Validation rules |
| `--dry-run` Stage 0 per-candidate surveys (`gh issue view` / `gh pr view`) | Dry-run gate, Procedures |

**Keep on plain `gh` (deliberately uncached — do NOT wrap these):**

| Call site | Why it must be live |
|---|---|
| Per-issue pre-flight step 1 (`gh issue view N --json state,labels,closedByPullRequestsReferences`), the timeline existing-PR probe, and the follow-up `gh pr view` routing read | **Claim arbitration.** A 30s-stale `loom:building` / open-PR view is exactly the window a competing Builder's claim lands in — the failure mode is a duplicate builder on a claimed issue |
| Mode C's C0 per-PR pre-flight (`--json number,state,labels,closingIssuesReferences`) | Same: routes a PR to Judge/Doctor/Merge; must see another session's just-written verdict |
| Step 5's checkpoint-divergence recheck (`gh pr view <PR> --json labels`) | Its entire purpose is detecting that a concurrent process moved the PR on |
| Step 7's overlap probe (`gh pr view X --json files`) and `mergeStateStatus` recheck | **Merge gating** — the last read before an irreversible merge. A failed probe call must be observed as a failure (exit status), not silently coerced into an empty-and-therefore-disjoint result — see step 7's `git diff` local fallback (#6390) |
| The `--dry-run` "Verifying nothing mutates" before/after reads | **Differential check** — the identical command runs twice around the operation under test; a cache hit would return the "before" value and make the check vacuously pass |

**Writes stay literal `gh`.** Never wrap `gh issue edit` / `gh pr comment` in
`"$GH_READ"` — the destructive-command guard hooks pattern-match the literal
command text and a wrapped form slips past them. After a mutation this sweep
made, drop the cache instead: `"$GH_READ" --clear-cache` (a local `/tmp`
sweep, zero API cost) so a later cached read cannot return pre-write state.

Full policy, TTL/invalidation semantics, and manual verification steps:
`.loom/docs/gh-cached.md` (source: `defaults/docs/gh-cached.md`).

