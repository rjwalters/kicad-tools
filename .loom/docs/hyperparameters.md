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
      "maxAdmissionsPerTick": 3
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
| `dispatch` | `maxConcurrent` | 3 | 1–256 | Sweep concurrency ceiling (dynamic cap stays bounded by disk/RAM) | `autonomous.workFinder.maxConcurrent` | `LOOM_WORK_FINDER_MAX_CONCURRENT` |
| `dispatch` | `maxAdmissionsPerTick` | 3 | 1–64 | Ramp cap: new sweeps admitted per tick (#4234) | `autonomous.workFinder.maxAdmissionsPerTick` | `LOOM_WORK_FINDER_MAX_ADMISSIONS_PER_TICK` |
| `lifecycle` | `leaseTtlMinutes` | 15.0 | >0–1440 | Lease-freshness TTL (#6286) | — | `LOOM_LEASE_TTL_MINUTES` |
| `lifecycle` | `idleExitMinutes` | 60 | 1–10080 | Idle-exit turnaround (#4467) | `autonomous.idleExit.idleMinutes` | `LOOM_AUTONOMOUS_IDLE_EXIT_MINUTES` |
| `rework` | `buildBackoffHigh` | 40 | 1–100000 | PR-debt level that engages the build back-off (#9410) | `autonomous.workFinder.buildBackoff.high` | — |
| `rework` | `buildBackoffLow` | 25 | 0–100000, `< high` | PR-debt level that releases it | `autonomous.workFinder.buildBackoff.low` | — |

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

- Every tranche-1 field **hot-applies** (#9768): the work-finder trio, idle
  exit, and the backoff pair re-read config every tick, and the lease TTL
  re-resolves from the layer on every lease check — a committed-block edit
  lands without a daemon restart. (The single-knob env vars are process
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
(`env-vector | config | legacy | default`) — the first thing to check when
an injected vector "didn't take".

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
| `queue` | Put the approved head on the forge's merge queue. This is dormant; see below. |

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

**Dormant.** Queue execution is compiled off (`QUEUE_EXECUTION_ENABLED = false`).
Under `direct`, `enqueue` and `dequeue` refuse with `NOT_QUEUE_MODE` before any
forge call. Under `queue`, they refuse with `EXECUTION_DORMANT` until #9978's
later phases install the lifecycle safety contract and the required
merge-group checks. No role or script invokes these verbs yet.

## Tranche roadmap

Tranche 1 (this issue) consolidates the seven fields above. Later tranches
migrate the remaining tunables — host-breaker thresholds, admission-brake
load, merge-train bounds, role execution budgets — onto the same surface.
Adding a field is: one struct entry + range check in `hyperparams.rs`, one
consumer overlay, one row in the table above.
