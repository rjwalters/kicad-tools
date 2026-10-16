# Unified Hyperparameters

**Issue #9683.** One typed, validated surface for Loom's operational
tunables — the knobs that govern dispatch cadence, concurrency, lease
lifetimes, and review-debt backoff — with run-level provenance (a digest
stamped onto every telemetry span) and a programmatic override vector for
external optimizers (CMA-ES, Bayesian search).

Implementation: `loom-daemon/src/hyperparams.rs`. Consumers overlay their
legacy config readers (`work_finder/config.rs`, `work_finder/build_backoff.rs`,
`idle_exit.rs`, `claim_reconciliation.rs`) — the legacy homes keep working.

## The config block

Live in `.loom/config.json` under the top-level `"hyperparameters"` key
(same tier-merge resolution as every other config key: machine defaults →
host config → repo config — see `config_resolver.rs`).

```json
{
  "hyperparameters": {
    "dispatch": {
      "tickIntervalSecs": 60,
      "maxConcurrent": 3,
      "maxAdmissionsPerTick": 3,
      "backlogExponent": 0,
      "busyExponent": 0
    },
    "lifecycle": {
      "leaseTtlMinutes": 15,
      "idleExitMinutes": 60
    },
    "rework": {
      "buildBackoffHigh": 40,
      "buildBackoffLow": 25
    }
  }
}
```

### Fields

Every field is optional; absent fields fall through the precedence chain
below. Types/ranges are strict **on this surface** (see Validation).

| Group | Key | Default | Range | Governs | Legacy home | Single-knob env |
|---|---|---|---|---|---|---|
| `dispatch` | `tickIntervalSecs` | 60 | 5–3600 | Work-finder tick cadence | `autonomous.workFinder.intervalSecs` | `LOOM_WORK_FINDER_INTERVAL_SECS` |
| `dispatch` | `maxConcurrent` | 3 | 1–256 | Sweep concurrency ceiling (dynamic cap stays bounded by disk headroom) | `autonomous.workFinder.maxConcurrent` | `LOOM_WORK_FINDER_MAX_CONCURRENT` |
| `dispatch` | `maxAdmissionsPerTick` | 3 | 1–64 | Ramp cap: new sweeps admitted per tick (#4234) | `autonomous.workFinder.maxAdmissionsPerTick` | `LOOM_WORK_FINDER_MAX_ADMISSIONS_PER_TICK` |
| `dispatch` | `itemWeightRedMain` | 50 | 1–1000000 (integer) | Step engine item draw (#11896): priority factor of a red-main fix in `weight = actionable × priority × age`. Read per tick by the draw (`step_dispatch/item_draw.rs`) | — | — |
| `dispatch` | `itemWeightVeryImportant` | 8 | 1–1000000 (integer) | Item-draw priority factor of `loom:very-important`, after the very-important cap (#11896) | — | — |
| `dispatch` | `itemWeightImportant` | 3 | 1–1000000 (integer) | Item-draw priority factor of `loom:important` (#11896) | — | — |
| `dispatch` | `itemAgeCapDays` | 90 | 0–3650 (integer) | Most stage-age days the item-draw age term `1 + min(age, cap) / divisor` counts (#11896; 0 turns the term off) | — | — |
| `dispatch` | `itemAgeDivisorDays` | 30 | 1–3650 (integer) | Divisor of the item-draw age term (#11896) | — | — |
| `dispatch` | `backlogExponent` | 0 | 0–1 (float) | Item draw α (#12185): 0 keeps a repo's share fixed whatever its backlog, 1 makes the pool flat. See [The item draw](#the-item-draw-12185). Hot-applies | — | — |
| `dispatch` | `busyExponent` | 0 | 0–100 (float) | Item draw β (#12185): a repo's weight is divided by `(1 + in_flight_repo)^β`; 0 is off. Hot-applies | — | — |
| `lifecycle` | `leaseTtlMinutes` | 15.0 | >0–1440 | Lease-freshness TTL (#6286) | — | `LOOM_LEASE_TTL_MINUTES` |
| `lifecycle` | `idleExitMinutes` | 60 | 1–10080 | Idle-exit turnaround (#4467) | `autonomous.idleExit.idleMinutes` | `LOOM_AUTONOMOUS_IDLE_EXIT_MINUTES` |
| `rework` | `buildBackoffHigh` | 40 | 1–100000 | Per-repo PR-debt level that engages a repo's build back-off (#9410, #10624) | `autonomous.workFinder.buildBackoff.high` | — |
| `rework` | `buildBackoffLow` | 25 | 0–100000, `< high` | Per-repo PR-debt level that releases it | `autonomous.workFinder.buildBackoff.low` | — |
| `champion` | `prSlice` | 10 | 1–1000 | PR rows Champion handles before the promotion pass (#10753) | — | `LOOM_CHAMPION_PR_SLICE` |
| `champion` | `promotionSlice` | 3 | 1–100 | Fresh promotion verdicts per pause while PR rows remain | — | `LOOM_CHAMPION_PROMOTION_SLICE` |
| `champion` | `tier2Cap` | 2 | 0–100 (0 disables) | No-level promotions per repo per pass (old Tier 2 cap, mapped by population #11512) | — | `LOOM_CHAMPION_TIER2_CAP` |
| `champion` | `tier3Cap` | 1 | 0–100 | Unused since #11512; parsed for config compatibility (old Tier 3 per-pass cap) | — | `LOOM_CHAMPION_TIER3_CAP` |
| `champion` | `tier3BacklogCap` | 5 | 0–1000 | Unused since #11512; parsed for config compatibility (old Tier 3 backlog gate) | — | `LOOM_CHAMPION_TIER3_BACKLOG_CAP` |

**Retired draw keys** (accepted, ignored, never a startup error; `loom-daemon hyperparams --validate` and the daemon's startup log warn once per key):
`dispatch.drawTemperature` (the #11684 softmax temperature, retired by #11742)
and `dispatch.weightBonusRedMainFix` / `weightBonusVeryImportant` /
`weightBonusImportant` (the repo-level points, retired by #12185: the step tick
draws one item from one pool, so a repo no longer earns points for holding
urgent work; the item priority factors below do that job per item). Their
values are not range-checked, do not enter the digest and no longer change any
draw. Delete them from the config.

## The item draw (#12185)

The step tick draws **one item** from a single pool of every actionable item
across the host's repos (`step_dispatch/item_draw.rs`; design
[`work-finder-step-dispatch.md`](https://github.com/rjwalters/loom/blob/main/docs/design/work-finder-step-dispatch.md)
§1 ruling 1, §4.2). There is no separate repo draw. An item's weight is

```
w_item = w_repo × priority × age × S_r^(α−1) × (1 + in_flight_repo)^(−β)
```

and the draw picks an item with probability `w_item / Σ w_item` over the pool
(without replacement within the tick, so `S_r` and the counts are recomputed
for the items still in the pool at each draw). These are the draw's knobs,
exactly, and what raising each one does:

| Term | Knob | Home | Raising it |
|---|---|---|---|
| `w_repo` | the repo's base weight (default 1; 0 = never drawn) | the fleet roster (`workspaces.json`, set from the fleet store, #11103). **Not** in the hyperparameter block: it is per repo, not per host | draws more from that repo, in proportion |
| `α` | `dispatch.backlogExponent` (0–1, float, default 0) | hyperparameters | moves the pool from "a repo's share is its `w_repo`, split among its items" (0) toward "every item is as likely as any other, scaled by `w_repo`, so a repo's share grows with its backlog" (1). Spreads draws over more distinct items, which cuts claim collisions on small repos |
| `priority`, red main | `dispatch.itemWeightRedMain` (1–1000000, default 50) | hyperparameters | draws a verified red-main fix more |
| `priority`, `loom:very-important` | `dispatch.itemWeightVeryImportant` (default 8) | hyperparameters | draws it more (after the one-per-repo very-important cap) |
| `priority`, `loom:important` | `dispatch.itemWeightImportant` (default 3) | hyperparameters | draws it more |
| `age` | `dispatch.itemAgeCapDays` (0–3650, default 90), `dispatch.itemAgeDivisorDays` (1–3650, default 30) | hyperparameters | `age = 1 + min(stage_age_days, cap) / divisor`: a longer cap or a smaller divisor favours old items more; a cap of 0 turns age off |
| `in_flight_repo`, `β` | `dispatch.busyExponent` (0–100, float, default 0) | hyperparameters | draws a repo less the more of its items are being worked on right now. 0 is off |

`priority` is one factor, not a product: a red-main fix that is also
very-important weighs the red-main factor. `S_r` is the sum of `priority × age`
over the repo's items still in the pool. `n_items` (the repo's pooled item
count) is not a term; it is recorded beside each draw for plotting.

Reading the formula at its ends:

- **α = 0, β = 0** (the defaults) gives `w_repo × priority × age / S_r`: the
  distribution of the earlier two-stage draw with its repo-level bonuses at 0.
  A repo's share is `w_repo / Σ w_repo` however many items it holds.
- **α = 1, β = 0** gives `w_repo × priority × age`: a flat pool.

`in_flight_repo` counts the repo's items this host runs plus the live claims
peers advertise over the peer-claim view, read at the start of the tick and
not updated as the tick spawns. It is as fleet-wide as the peers' claim
adverts reach; a peer that does not advertise is not counted.

**Knobs live in fleet-gitops.** Set the six hyperparameter keys in
`fleet/defaults.json` or a host overlay like any other `hyperparameters` key
(they are in the strict validated block, enter `loom.hyperparams.digest`, and
are re-read every tick, so a fleet-gitops merge applies without a restart).
`dispatch.backlogExponent` must be in `[0, 1]` and `dispatch.busyExponent`
in `[0, 100]`; `--validate` rejects a value outside, a non-number, or an
unknown `dispatch` key. The exponents are read from the primary workspace's
layer, since the pool is host-wide; the item weights are read per repo.

**Replay.** The draw stays seeded (`loom-draw/v3`, per draw, no workspace in
the seed). `pick.decision`'s `item_draw` records both exponents, each repo's
`w_repo`, `in_flight` and `n_items`, the starting pool with every item's
`priority × age`, the repos a per-repo cap closed mid-tick, and every roll,
so a tick's picks replay from the record. `loom.dispatch.step` carries the
drawn item's draw weight, the pool total at that draw, and its repo's
`n_items` and `in_flight_repo` (`loom.step.weight`, `loom.step.total_weight`,
`loom.step.repo_items`, `loom.step.in_flight_repo`); `weight / total_weight`
is the item's selection probability.

The `champion` values enter the run digest. Champion's shell snippets read
the `LOOM_CHAMPION_*` env vars directly, so the resolver honours them at the
top tier (provenance `env`) with the shell's own parse: a non-negative
integer wins (`0` included, unclamped), empty/non-integer falls through. The
daemon does not yet export block/vector values into role sessions (follow-up
on #10753): until it does, a config/vector value is digested but only the env
var changes Champion's behaviour.

### Role-runner knobs (`autonomous.roleRunner.*`)

These are not hyperparameter-surface fields: they live under `autonomous.roleRunner`
(resolved per root, re-read every role-runner tick) and keep their own validation
(an invalid value drops to the default). The full table, with env overrides and
semantics, is in [`daemon-reference.md`](daemon-reference.md) (search for
`autonomous.roleRunner`). The ones that change how much work runs:

| Key | Default | Governs |
|---|---|---|
| `enabled` | `false` | Periodic standalone support-role runner on/off (#4015) |
| `maxConcurrent` | the 7 interval-default roles | Host-wide ceiling on concurrently-running role agents (#6102) |
| `roleMaxConcurrent` | `max(1, maxConcurrent / 2)` per role | Per-role budget under the ceiling (#9391) |
| `eventTriggers.enabled` | `false` | Event-driven curator/auditor/guide launches instead of every interval (#10816) |
| `eventTriggers.maxQuietSecs` | 86400 | A gated role that has not launched in this long launches anyway |
| `demandWidth.enabled` | `true` | Demand-weighted role admission (#9392); `false` restores Phase 1 admission |
| `demandWidth.perRun` | 3 | Queued PRs per role run in the PR-role width |
| `demandWidth.max` | 4 | Upper clamp on judge, doctor and champion width (still capped by the role budget) |
| `demandWidth.reserve` / `nonPrFloor` | `true` / 1 | Champion-first ceiling reservation, and the slots it always leaves for non-PR roles |
| `demandWidth.laneK` / `perRepoCap` / `doctorMaxPerRepo` | 10 / 3 / `perRepoCap` | Per-repository judge and doctor lanes (#10630, #10632) |
| `demandWidth.staleSecs` | 1800 | Demand-ledger entries older than this are stale (unobserved axis) |

`eventTriggers` and `demandWidth` change how many role records are written; see
[`telemetry-kind-pick-decision.md`](telemetry-kind-pick-decision.md).

## Precedence

```
single-knob env var                      (one-off operator override, one run)
  > $LOOM_HYPERPARAMS vector             (JSON, programmatic — optimizer loops)
  > "hyperparameters" config block       (committed, validated, canonical)
  > legacy autonomous.* config key       (still honored; block wins where both set)
  > built-in default                     (the constant each knob used before)
```

`$LOOM_HYPERPARAMS` is a JSON object whose keys may be nested
(`{"dispatch":{"maxConcurrent":6}}`) or flat dotted
(`{"dispatch.maxConcurrent":6}`). It requires no file edit, so an optimizer
loop can tune a whole run with one env export.

Notes:

- Tranche-1 fields **hot-apply** (#9768): `maxConcurrent`, the item-draw
  knobs (#12185), idle exit, and the backoff pair re-read config every tick,
  `maxAdmissionsPerTick` does too since #12090, and the lease TTL re-resolves
  from the layer on every lease check — a committed-block edit lands without
  a daemon restart. The exception is `dispatch.tickIntervalSecs`, which still
  needs a restart (#12090). (The single-knob env vars are process
  environment: fixed at launch, as always.)
- A legacy key and a block key may coexist; the block wins per field, the
  legacy key fills fields the block omits.

## Validation (fail fast at startup)

`hyperparams::startup_init` runs once at daemon startup (before any span
exists) and **aborts startup** when the hyperparameters surface — the
committed block and/or the env vector — has:

- an unknown group or unknown key inside a known group (typo catcher),
- a wrongly-typed value, or a value outside its documented range,
- a crossed `rework` pair (`buildBackoffLow >= buildBackoffHigh`).

The error names every offending dotted path, so an optimizer that samples an
invalid vector learns exactly which coordinate was rejected. An unparseable
`$LOOM_HYPERPARAMS` is likewise fatal at startup.

Legacy `autonomous.*` values are **never** gated by this — those keys keep
their own documented soft-fallback semantics (e.g. a crossed legacy backoff
pair falls back to 40/25 with a warning), so an existing committed config
can never start failing this gate after an upgrade.

## Run provenance

After validation, `startup_init` resolves the effective vector down the full
precedence chain and records a digest — `sha256:<hex>` over the vector's
canonical JSON — in a process global. The trace provenance stamper
(`telemetry/trace/provenance.rs`) then writes it as
**`loom.hyperparams.digest`** on every `loom.*` span, alongside
`loom.daemon.revision` and `loom.prompts.digest` (policy:
[trace-identity](trace-identity.md)). A run's telemetry is therefore
reproducible from the exact hyperparameter vector it ran under: re-set the
same `$LOOM_HYPERPARAMS` (or config block), get the same digest.

The digest covers the whole resolved vector including inherited defaults —
two runs with identical digests ran identical tunables, even if they got
there by different tiers.

## Inspecting & validating: `loom-daemon hyperparams`

```
$ loom-daemon hyperparams              # human-readable table + digest
$ loom-daemon hyperparams --json       # {"params":…, "sources":…, "digest":…}
$ loom-daemon hyperparams --validate   # run the startup gate without a daemon
```

`sources` reports, per field, which tier supplied it
(`env | env-vector | config | legacy | default`; `env` is champion-only) —
the first thing to check when an injected vector "didn't take".

`--validate` runs the same strict gate daemon startup enforces (unknown
keys, types, ranges, crossed backoff pair, unparseable vector) against a
workspace **without booting one** — a config lint for a proposed
`.loom/config.json` edit or `$LOOM_HYPERPARAMS` vector. Exit 0 and
`hyperparams: OK` when valid; non-zero naming every offending path
otherwise. Combine with `--json` for a machine-readable violations array
(#9768).

## Optimizer recipe (CMA-ES)

```bash
# 1. Baseline
digest=$(loom-daemon hyperparams --json | jq -r .digest)

# 2. Sample a candidate vector and run the fleet under it
export LOOM_HYPERPARAMS='{"dispatch.maxConcurrent": 5, "rework.buildBackoffHigh": 55}'
restart-daemon                        # startup validates; invalid samples fail fast

# 3. Confirm the injection took
loom-daemon hyperparams --json | jq '.sources["dispatch.maxConcurrent"]'  # env-vector

# 4. Attribute outcomes by digest: every span of the run carries
#    loom.hyperparams.digest — group cycle-time / token-efficiency metrics on it.
```

Invalid samples abort startup with the offending path named — treat a
non-booting daemon as an infeasible point, not a crash.

## Merge-queue mode: enablement prerequisites and rollback

Queue mode (#9978) is not a hyperparameter. It is a per-repository merge-mode
setting, defined by Phase A (#10255), that defaults to direct. Before any
repository switches to queue mode, it must pass
`loom-daemon merge-group-ci eligibility`. That check requires an
organization-owned repository, push permission, an active `merge_queue` rule,
required checks, and a workflow audit proving that every required suite
validates the combined merge-group tree. The switch also needs operator
authorization, because a ruleset change is a protected-settings change. To
roll back, set the mode back to direct and have the operator remove the
`merge_queue` rule. The `merge_group` workflow trigger can stay, because it is
inert without a queue. Full checklist, evidence and rollback steps:
[merge-queue-ci](merge-queue-ci.md).

### The setting: `champion.mergeMode` (#10255)

There is one key, `champion.mergeMode`. It is a top-level config key, not part
of the `"hyperparameters"` block, and the strict gate described above does not
cover it.

| Value | Meaning |
|---|---|
| `direct` (default) | `merge-pr.sh` merges the approved PR itself. This is today's behaviour. |
| `queue` | Put the approved head on the forge's merge queue. Opt-in; see below. |

Precedence is `$LOOM_MERGE_MODE` (an empty value counts as unset), then the
tier-merged config (`config_resolver.rs`, with `.loom-local` above
`.loom-project` above `.loom/config.json` above machine defaults), then
`direct`. Any other value, including `"Queue"`, `"queued"` or a non-string, is
an error that names the value and where it came from. It **never** falls back
to `direct`.

`loom-daemon forge merge-queue` exposes the typed controls in
`loom-daemon/src/forge_merge_queue/`:

- `mode` prints the resolved mode, its source and the execution gate.
- `preflight [--repo] [--branch]` is read-only. It reports one of these
  distinct kinds: `UNSUPPORTED_FORGE`, `UNSUPPORTED_REPOSITORY`,
  `MISSING_QUEUE_RULE`, `MISSING_REQUIRED_CHECKS`, `CONFIG_INACCESSIBLE` or
  `RATE_LIMITED`.
- `status <pr>` shows the PR's head and its queue entry.
- `enqueue <pr> --approved-sha SHA` always sends `expectedHeadOid` set to the
  approved head and never sends `jump`.
- `dequeue <pr>` removes the PR from the queue.

Enqueue and dequeue are idempotent: if the PR is already queued at the approved
head, or is not queued at all, nothing is sent. A head mismatch is reported and
never retried with the refreshed SHA. Diagnostics redact anything that looks
like a token.

Exit codes are 0 for ok, 1 for failed or not capable, 2 for an invalid config,
3 for could-not-determine, and 4 for a refusal before any forge call.

**Opt-in.** Queue execution is compiled on (`QUEUE_EXECUTION_ENABLED = true`,
#12323). Under `direct` (the default), `enqueue` and `dequeue` refuse with
`NOT_QUEUE_MODE` before any forge call. Under `queue`, they reach the forge;
the repo also needs the `merge_group` check workflow
(`defaults/optional/github-workflows/merge-group-check.yml`).

## Tranche roadmap

Tranche 1 (this issue) consolidates the seven fields above. Later tranches
migrate the remaining tunables — host-breaker thresholds, admission-brake
load, merge-train bounds, role execution budgets — onto the same surface.
Adding a field is: one struct entry + range check in `hyperparams.rs`, one
consumer overlay, one row in the table above.
