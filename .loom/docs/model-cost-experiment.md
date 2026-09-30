# Model-Cost Experiment

Reference detail for the `/loom:sweep` model-cost A/B experiment (#3725). Treated
as the authoritative behavior contract by
[`docs/model-selection-retune.md`](https://github.com/rjwalters/loom/blob/main/docs/model-selection-retune.md).

### Model-Cost Experiment (canary A/B, #3725)

`/loom:sweep` can instrument a run to produce the balanced A/B evidence the
measurement-gated Builder `opus → sonnet` retune (#3718) needs — you cannot
generate it by observation alone, since passive collection only ever measures
whatever the current Builder default is. **Off by default; zero behavior change
unless you turn it on.** Tri-state `sweep.modelExperiment` /
`LOOM_MODEL_EXPERIMENT` (env-over-config, string-valued like `guards.rmScope`):

| Mode | Behavior |
|------|----------|
| `off` (default) | No instrumentation. No `.loom/stats/` file. Byte-for-byte unchanged. |
| `observe` | Passive: one JSONL record per phase to `.loom/stats/sweep-model-stats.jsonl`. No model forcing. Safe anywhere. |
| `experiment` | Active A/B: Builder forced to the per-issue arm's model. **Canary-only.** |

```bash
# Observe on any sweep (no behavior change, just records the outcome-chain):
LOOM_MODEL_EXPERIMENT=observe claude -p "/loom:sweep 123" --dangerously-skip-permissions

# Experiment on a canary (must confirm the canary — else it downgrades to observe):
LOOM_MODEL_EXPERIMENT=experiment LOOM_MODEL_EXPERIMENT_CANARY=1 \
  claude -p "/loom:sweep 123" --dangerously-skip-permissions
```

**Two arms** map onto #3718's inequality: **Arm A = opus-first** (Builder→opus),
**Arm B = sonnet-first + escalate-on-Judge-rejection** (Builder→sonnet, escalating
via the `sweep.escalation` ladder). Arm assignment is a **deterministic,
resume-safe** function of the issue number, **stratified by the Curator complexity
marker** (#3702) so both arms see a comparable difficulty mix — a killed-and-resumed
sweep re-lands the same arm. In `experiment` mode the tier-2.5 complexity bump is
**suppressed** (the marker is used only as the stratification key), so a
`complex`-marked issue on Arm B stays sonnet and the A/B is not confounded.

## N configurable arms (`sweep.modelExperimentArms`, #9122)

The built-in A/B pair above is the **default**, not the limit. Declare 2+ named,
weighted arms and the experiment becomes an N-way comparison over the same
telemetry plumbing — records, harvest, and `sweep-outcomes summary --group-by
arm` are already arm-name-agnostic:

```json
{
  "sweep": {
    "modelExperiment": "experiment",
    "modelExperimentArms": [
      { "id": "OPUS",   "model": "opus",   "weight": 1 },
      { "id": "SONNET", "model": "sonnet", "weight": 1 },
      { "id": "HAIKU",  "model": "haiku",  "weight": 8 }
    ]
  }
}
```

- `id` is the arm's reporting identity (upper-cased to match the stats store's
  own convention, so a configured id round-trips record → harvest unchanged).
  Ids must be unique.
- `model` is a logical alias or a pinned ID, resolved through the **same**
  resolver `resolve-model.sh` uses (the #4060 contract), so an arm and the
  escalation ladder can never disagree about what `opus` means.
- `weight` is a relative selection weight (> 0, default `1`); weights need not
  sum to anything. Above, `HAIKU` draws 8/10 of in-experiment issues.

Assignment stays **deterministic, resume-safe, and stratified** exactly as the
2-arm original: it is a pure function of `(issue number, complexity stratum)`,
and each stratum converges to the configured weights independently.

**Absent this key (or if it is rejected) behavior is byte-for-byte the
pre-#9122 A/B pair.** Rejection is **loud on stderr and falls through** to the
built-in pair — never a hard sweep failure, mirroring `resolve-tier-model.sh`'s
No-Fable refusal. A roster is rejected **whole** (a partially-honored roster
would silently change the weights you asked for) when any of these holds:

| Rejection | Why |
|---|---|
| Not an array, or fewer than 2 arms | An "experiment" with one arm is not an experiment — use a tier pin |
| An entry that is not an object, or a blank `id`/`model` | Unusable |
| A duplicate `id` | Arm ids are the stats store's grouping key |
| A `weight` that is not a finite number > 0 | Cannot be sampled |
| A `model` that names **or resolves to** `fable` | **No-Fable bound (#3702)** — checked on both sides of alias resolution, so an arm cannot reach fable through a `sweep.modelAliases` indirection |
| A `runtime` naming anything but `claude` | Phase 1 is **Claude-only**; see "Deferred" below |

## Budget-fraction cap (`sweep.modelExperimentBudgetFraction`, #9122)

By default `experiment` mode forces an arm on **every** eligible issue. The
budget fraction bounds what share of them receive *any* forced arm, so a canary
can spend only part of its budget on the experiment:

```bash
# Spend ~30% of eligible issues on the experiment; the other ~70% run normally.
LOOM_MODEL_EXPERIMENT=experiment LOOM_MODEL_EXPERIMENT_CANARY=1 \
LOOM_MODEL_EXPERIMENT_BUDGET_FRACTION=0.3 \
  claude -p "/loom:sweep 123" --dangerously-skip-permissions
```

Precedence is the usual **env > config > default**: `LOOM_MODEL_EXPERIMENT_BUDGET_FRACTION`
→ `sweep.modelExperimentBudgetFraction` → **`1.0`**. A malformed or
out-of-`[0.0, 1.0]` value warns and falls back to `1.0`; it never fails a sweep.
**The shipped default `1.0` reproduces today's always-forced behavior exactly**
(it does not even evaluate the sampling hash) — picking a real fraction for a
live canary is a spend decision, the same class as the `LOOM_MODEL_EXPERIMENT_CANARY`
confirmation itself.

An issue sampled **out** of the experiment:

- gets **no arm** (`arm` null in its stats record — the existing `observe`-mode
  null-arm convention, not a new state; `assign-arm` prints `none -`),
- proceeds with **unmodified** tier-2.5/tier-3 model resolution, exactly as if
  the experiment were off for that issue,
- and gets its own loud `NOT IN EXPERIMENT` banner naming the fraction.

**The in/out decision is a separate hash from the arm choice, on purpose.** The
two use distinct domain separators over the same `(issue, stratum)` key, so
widening or narrowing the fraction mid-canary re-partitions *who is in the
experiment* **without reshuffling which arm** an already-in-experiment issue
lands on — the arm attribution of every record already written stays valid.

## Deferred (not in #9122 Phase 1)

- **Non-Claude / multi-runtime arms** (GLM via OpenCode, Pi, …) — Phase 2. An
  arm carrying a non-`claude` `runtime` is refused today rather than silently
  ignored or dispatched, which is exactly this scope boundary made visible.
- **Multi-judge blind cross-model evaluation** (self-preference-bias
  mitigation) — Phase 3. Distinct from the deferred same-model
  multi-dimension fan-out sketch (#3739/#3748).
- **SigNoz `loom.experiment.arm` / `gen_ai.request.model` span attributes and a
  cost/quality Pareto view** — Phase 4.
- **Fleet-wide / workspace-level arm assignment** is a different axis entirely,
  tracked separately as #8055. This experiment's unit of assignment stays
  **per-issue**.

**Guardrails:** off by default; `observe` safe anywhere; `experiment` refuses to
run on a non-canary target and loudly downgrades to `observe` unless an
**uncommitted** signal confirms a canary — the `LOOM_MODEL_EXPERIMENT_CANARY=1`
env var or the gitignored `.loom/CANARY` sentinel file. The confirmation must be
uncommitted **by design** (#3731): the committed `sweep.modelExperimentCanary`
config flag is **no longer** an accepted confirmation (it would propagate with a
copied config via `defaults/`, firing experiment on production). A git-tracked
`.loom/CANARY` is likewise refused. The `sweep.modelExperiment` *mode* may still
live in committed config — it is inert without the uncommitted confirmation. A
loud startup banner names the active mode, the canary confirmation source, and,
in `experiment`, the arm assigned to each issue. `.loom/stats/` and `.loom/CANARY`
are gitignored.

**Harvesting the evidence:**

```bash
./.loom/scripts/agent-metrics.sh --model-experiment --archive-dir "$LOOM_TRANSCRIPT_ARCHIVE"
```

The load-bearing signal is the **deterministic outcome-chain** (arm / model /
attempt / Judge verdict / Doctor-cycle count / complexity) — that alone answers
#3718's inequality (first-attempt Judge-pass rate + mean Doctor cycles × model
price), and it is stamped into the durable store per phase.

**On token fidelity — read this before trusting a cost number.** There is **no
per-phase real-token split at the Task-result boundary** (the harness does not
surface per-subagent `usage` when a Task returns). Instead, **exact per-role cost
is recovered at harvest time** by parsing each role subagent's durable
`agent-<id>.jsonl` transcript — each Builder / Judge / Doctor invocation is its own
transcript with full per-message `usage` (input/output + cache read/creation
split + model), so this is true per-role granularity, not a whole-process
aggregate. Cost uses the same cache-aware per-model pricing as `loom-daemon`'s
`resource_usage.rs`. Harvest locates transcripts through the #3726 transcript
archive's `agent-<id>`-keyed index (`--archive-dir` = `LOOM_TRANSCRIPT_ARCHIVE`),
joined on the agent-id stamped in each stats record. Over a **multi-day canary**,
run harvest periodically (cron) so usage is extracted before `~/.claude/projects`
is pruned — or rely on the #3726 archive as the durable backstop. Each record
carries a `token_fidelity` tag (`transcript` | `sweep-aggregate-log` | `none`) so
you know exactly what a cost figure came from.

**Daemon detached-child path (verified against on-disk transcripts).** The
role-subagent transcripts of a daemon-dispatched `claude -p "/loom:sweep N"`
child land under that child's own
`${CLAUDE_CONFIG_DIR:-$HOME/.claude}/projects/<cwd-slug>/<child-session-uuid>/subagents/agent-<id>.jsonl`
tree — the **durable** location, not the ephemeral `/tmp/.../tasks/` scratch —
and each carries the full per-message `usage` (input/output + cache split) and
`model`. Confirmed present on disk for real detached-child sessions, so they are
archivable/harvestable via the same #3726 periodic sync. What the daemon reaper
does **not** yet know is the child's session-uuid, so it cannot trigger a precise
single-session archive on exit — the cron periodic sync is the backstop, exactly
as for the completion hook (see "Session Transcript Archival" in
`sweep-execution-model.md`).
