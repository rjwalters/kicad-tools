# Weighted role draw (`runtimes.roleDraw`, #11726)

`runtimes.rolePreference` picks a stage's runtime by an ordered list: the first
available tap wins. `runtimes.roleDraw` replaces that, per stage, with a
**weighted random draw** of runtime (and model) **per attempt**. It lets the
fleet send a small, measured share of a stage to an experimental provider and
make sure a failed or blocked attempt is followed by a different table.

Why the ordered list was not enough (2026-10-12): Champion, Judge, Curator and
the other read-only roles were Codex-first. Stale Codex session containers made
a guard hook deny every tool call; each run exited cleanly, so the Claude entry
below Codex never fired, and Champion merged nothing for about 30 hours. With a
draw, a broken provider degrades only its sampled share, and attempt 2 draws
from a table that need not contain it.

## Config

```jsonc
"runtimes": {
  "roleDraw": {
    "judge": [
      { "attempt": 1, "draw": { "claude/opus": 80, "claude/fable": 10, "codex": 10 } },
      { "attempt": 2, "draw": { "claude/fable": 100 } }
    ],
    // Equivalent object form, keyed by attempt number:
    "curator": { "1": { "codex": 10, "claude": 90 }, "2": { "claude": 100 } }
  },
  "excludeProviders": ["codex"]
}
```

- **Weights** are non-negative integers (percent or per mille, normalised):
  `P(candidate) = w / Σw`. Weight `0` is off.
- **Attempt N** uses the highest configured attempt `<= N`. A role whose
  tables start above its current attempt has no draw on that attempt.
- **`excludeProviders`** removes a runtime (or one exact candidate key) from
  every table on the host and renormalises the rest, so a fleet-wide weight
  change cannot reintroduce Codex on a host that excludes it. It applies to
  `roleDraw` tables only, not to `rolePreference` lists.
- `_`-prefixed keys are comments and ignored, as elsewhere in the fleet config.

## Candidate keys

`<runtime>[:<profile>][/<model-or-profile>]`

| Key | Means | `rolePreference` equivalent |
|---|---|---|
| `claude`, `codex` | The bare runtime; the model resolves as today (`roleModels`, shipped default, CLI default). | `"claude"` |
| `opencode:zai-flash` | Runtime plus model profile (the tap's provider and credential pool). | `{"runtime":"opencode","modelProfile":"zai-flash"}` |
| `opencode/zai-flash` | For a **native** runtime the `/` segment is its model profile: same as above. | same |
| `claude/opus`, `claude/fable` | Runtime plus a model pinned with `--model` on the role-tick launch. | none (`rolePreference` has no model axis) |

The tap a candidate launches on is `<runtime>[:<profile>]`, which is what
`preference_tap`, the launch-failure hold and availability use. Two candidates
on one tap (`claude/opus`, `claude/fable`) share its availability.

## Layers and per-host override

The daemon reads each config tier separately: machine defaults
(`~/.local/share/loom/config/defaults.json`, where fleet-gitops renders
`config.defaults` plus host-class and host overrides), `.loom/config.json`,
`.loom-project/project.json`, the per-repo fleet tier
`.loom-local/fleet-repo.json` (the fleet config's `repos[].config`, written by
the fleet sync, #11738), then `.loom-local/local.json`. A higher tier
replaces a role's table **per attempt**, so a host can replace `judge` attempt
1 without restating attempt 2 or any other role. `null` at a higher tier clears
that role (or, for `roleDraw: null`, every role). `excludeProviders` follows the
ordinary rule: the highest tier that sets it wins.

Inside the machine-defaults file the tiers have already been merged by
fleet-gitops, whose generic deep merge replaces lists. There, use the
**object form** to override one attempt: it merges per attempt and per
candidate (set a candidate to `0` to switch it off on that host).

## Resolution

1. An operator pin (`LOOM_RUNTIME`, `LOOM_RUNTIME_<ROLE>`, an explicit
   per-dispatch runtime) still wins outright; no draw happens.
2. If the role has a table for its attempt, the daemon draws a **weighted
   random order** of the drawable candidates and runs the ordinary preference
   walk over it: admission, credential availability, the launch-failure hold
   and the backstop ceiling. The first candidate that passes launches. An
   unavailable candidate is excluded and the rest are renormalised, because the
   first available entry of a weighted random order is distributed exactly as
   `w / Σw` over the available ones.
3. With no table, `rolePreference` / `preference` / static resolution decide,
   unchanged.
4. A table that is **empty after exclusions** is a non-blocking warning (daemon
   log and the explain view). The stage has nothing to run on, so its tick is
   a pre-spawn skip whose diagnostic says so, like a wholly unavailable list.

The backstop ceiling (`runtimes.backstopCeiling.appliesFrom`) is positional: it
governs tier N of the drawn order, so a drawn first pick is ungoverned and only
a fall-through to a later pick is bounded.

There is no content validation: any mix of providers is allowed, and a
malformed entry (non-integer weight, bad key, unknown role) is skipped with a
warning instead of failing the launch.

## Attempts

A role tick's attempt is `1 +` the number of consecutive **launched** ticks of
that role in that workspace that failed or were blocked. That includes a tick
where a guard hook denied every tool call (`hook-denied`), a sandbox no-op, a
toolless launch, a non-zero exit and a timeout. A success resets it to 1. A
pre-spawn skip counts neither way. The count lives in daemon memory, so a
restart starts every role at attempt 1.

Sweep dispatch and `worker run` phases draw attempt 1. The `/<model>` segment
is pinned only on role ticks; a sweep uses the drawn runtime and profile with
its own model resolution.

## Reproducibility and telemetry

Each draw is seeded by the first 8 bytes (big-endian) of
`sha256("loom-role-draw/v1:" + minute + ":" + host + ":" + workspace + ":" +
role + ":" + attempt)`: the workspace draw's `loom-draw/v1` scheme (#11683)
plus the workspace directory name, role and attempt. The randomness is the same
seedable `PickRng`.

The decision record carries role, attempt, the table attempt and the tier that
set it, the drawable table, entries that were off and why, the seed and its
inputs, each roll (`Σw`, roll, pick), passed-over picks and the final pick:

- on the `# LOOM_RUNTIME_PREFERENCE` marker as `source=role-draw draw=attempt:…,
  table:a2@repo-config,seed:…,sum:…,roll:…,pick:…`;
- on `role_tick.outcome` as `role_draw` (see
  [telemetry-schema.md](telemetry-schema.md)), mapped to `loom.role_draw.*`
  attributes. That record stays in the host's `role-tick-telemetry.jsonl`
  and is not shipped to the collector.

What reaches SigNoz (#11726 follow-up):

- **Role ticks:** the tick's exported `loom.role_attempt` span (`loom.sweep_id`
  `role-*`) carries `loom.role_draw.pick`, `.attempt`, `.table_attempt`,
  `.layer` and, when the pick pins one, `.model`. Success and blocked rates
  group by `loom.role` × `loom.role_draw.pick` × `loom.result` from that span.
  A tick that `rolePreference` decided carries none of these keys.
- **Sweeps:** `sweep.outcome` carries `loom.role_draw.pick` and
  `loom.role_draw.model_pinned` (always `false` today, because a sweep does not
  pin the drawn `/model`). Both are absent when no `roleDraw` table decided the
  sweep.

## Explain view

```sh
loom-daemon roles draw --explain            # text, current directory
loom-daemon roles draw --explain --json --workspace <repo>
```

For every stage it prints each attempt's effective table on this host, each
candidate's weight and probability before availability, the tier that set the
table, the entries `excludeProviders` or weight 0 removed, and an empty-table
warning. A stage without `roleDraw` shows the `rolePreference` list or static
binding that still decides it.

## Compatibility

- A daemon that predates `roleDraw` ignores `runtimes.roleDraw` and
  `runtimes.excludeProviders`: nothing parses `runtimes` strictly (the fleet's
  `runtimes._*_note` keys already depend on that). It keeps using
  `rolePreference`, so keep `rolePreference` in place until every host runs a
  daemon with this feature.
- A role without a table behaves exactly as before.
