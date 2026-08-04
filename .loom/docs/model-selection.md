# Model Selection Strategy

How Loom resolves each worker's model, the Judge-rejection escalation ladder, and
the suggested-model defaults by role. Retuning these defaults is measurement-gated
— see [`docs/model-selection-retune.md`](https://github.com/rjwalters/loom/blob/main/docs/model-selection-retune.md).

### Model Selection Strategy

Model selection is a first-class orchestration concern (issue #3477, Phase 1). Each worker's model is resolved through a fixed precedence chain — highest first:

1. **Explicit dispatch param** — `mcp__loom__dispatch_sweep`'s optional `model` argument (daemon path), an explicit `--model` flag passed to `spawn-claude.sh` / `claude-wrapper.sh`, or an operator-requested model for an in-session sweep.
2. **Workspace override** — `.loom/config.json` → `terminals[].roleConfig.model` (optional). Pin exact IDs here (e.g., `claude-sonnet-4-6`) when your workspace needs deterministic cost/behavior.
3. **Role default** — `.loom/roles/<role>.json` → `suggestedModel` (ships as an alias). The `/loom:sweep` skill passes the resolved model to role subagents via the Task tool's `model` parameter.
4. **Session default** — when nothing above resolves, NO `--model` flag (and no Task `model` param) is emitted at all, and the worker inherits the parent session/CLI default. This is the zero-config behavior: nothing configured means nothing changes.

The spawn plumbing also honors a `LOOM_MODEL` environment variable (`spawn-claude.sh`, `claude-wrapper.sh`): it is injected as `--model <value>` unless an explicit `--model` is already present in the args. Retries inside `claude-wrapper.sh` always reuse the same model — transport-level failures (token exhaustion, crashes, 5xx) are not quality signals and never change the model.

**Escalation on Judge rejection (`sweep.escalation`, Phase 2, issue #3481)**:

The `/loom:sweep` orchestrator escalates one rung up a capability ladder when the Judge rejects a PR (`loom:changes-requested`) and a Doctor is dispatched to address the feedback. The escalation decision is made by the sweep orchestrator at Doctor-dispatch time — never by `claude-wrapper.sh` retries, never by worker self-assessment. Mode C (`--prs`) inherits the same rule for its Doctor phase (step C1b).

The ladder is configured in `.loom/config.json`:

```json
{
  "sweep": {
    "escalation": ["sonnet", "opus"]
  }
}
```

| Value | Behavior |
|-------|----------|
| Key absent | Default ladder `["sonnet", "opus"]` applies |
| `[]` or `false` | Escalation disabled entirely (pure Phase 1 behavior) |
| Non-empty array | As configured; rungs accept aliases or pinned IDs |

**Precedence interaction**: escalation replaces only tier 3 (`suggestedModel`) / tier 4 (session default) resolution for the rejection-triggered Doctor. Tier 1 (explicit dispatch param) and tier 2 (`roleConfig.model` workspace pin) always win — pins are never overridden. `ladder[0]` never overrides anything either: first attempts of every role use the unmodified precedence chain, and the ladder only fires on rejection (the rejection-triggered Doctor gets `ladder[1]`).

**Cap interaction**: escalation composes with — and does not extend — the configurable Doctor→Judge cycle cap (`sweep.max_doctor_cycles`, default 1; issue #3668). The ladder is consumed as `ladder[min(attempt - 1, len - 1)]`, so raising the cap above 1 (or granting the default-cap distinct-defect grace cycle) activates deeper rungs automatically. At the default cap of 1, a second Judge rejection blocks the PR rather than dispatching another Doctor — unless that second rejection is a demonstrably distinct defect from the first, in which case the orchestrator may grant one logged, single-use grace cycle (never on an operator-raised cap). The sweep checkpoint's optional `attempt` field (`sweep-checkpoint.sh write N doctor-done --attempt 2`) records the cycle count (2 = first Doctor cycle, 3 = second, …); absent means attempt 1, and legacy checkpoints without the field read cleanly.

**Suggested models by role** (`suggestedModel`, live as the role-default tier):

| Role | Model | Rationale |
|------|-------|-----------|
| Builder | `opus` | Complex implementation requires deep reasoning |
| Judge | `opus` | Code review needs thorough understanding |
| Curator | `sonnet` | Issue enhancement is structured |
| Doctor | `sonnet` | PR fixes are usually targeted and scoped |
| Architect | `opus` | System design requires sophisticated thinking |
| Hermit | `sonnet` | Code removal analysis is pattern-based |
| Champion | `sonnet` | Proposal evaluation has clear criteria |
| Guide | `sonnet` | Triage is systematic |
| Driver | `sonnet` | General-purpose default |

> **Retuning these defaults is measurement-gated.** Whether to flip a role's
> default `opus → sonnet` ("cheap-first") is decided by measured data, not edited
> blind — see [`docs/model-selection-retune.md`](https://github.com/rjwalters/loom/blob/main/docs/model-selection-retune.md)
> (upstream Loom repo) for the decision inequality and the `agent-metrics.sh
> --by-model` (#3482) gating procedure. Builder is the only real candidate; no
> default has been flipped.

**Valid model values**: aliases (`haiku`, `sonnet`, `opus`) or pinned model IDs (e.g., `claude-sonnet-4-6`).

- **haiku**: Fast, cheap - for simple status checks and monitoring
- **sonnet**: Balanced - for structured tasks with clear criteria
- **opus**: Most capable - for complex reasoning and implementation

**Aliases vs pinned IDs**: shipped role JSONs use aliases so defaults stay sensible across model releases with zero maintenance. The GitHub Actions cron workflows (`.github/workflows/loom-*.yml`) are the exception — they pin exact IDs because scheduled support roles are predictable, cost-sensitive load and a stale pin is visible and cheap to bump in the consuming repo.

> **Logical-tier resolution (`sweep.modelAliases`, issue #3982).** A logical alias
> is not always current on the wire: the bare `opus` alias still resolves to a
> **previous-generation** model (`claude-opus-4-8`) while `sonnet`/`fable` resolve
> to the current generation, which would make the escalation ladder
> `sonnet → sonnet@xhigh → opus → fable` step *down* a generation at the `opus`
> rung. So every consumer keeps naming `opus` and a **single indirection point**
> maps the logical tier to the concrete ID the dispatch should use — the
> `/loom:sweep` skill via `./.loom/scripts/resolve-model.sh` and `loom-daemon`
> via `resolve_dispatch_model`. (Issue #4809:
> a **daemon-dispatched** single-issue sweep resolves through the sibling
> `resolve_autonomous_dispatch_model`, which inserts the model-cost A/B
> experiment's forced arm — when resolved-`experiment` mode confirms a canary —
> ahead of `resolve_dispatch_model`'s own config/default sub-tiers; an explicit
> dispatch `model` param still wins over both.) The shipped map
> pins only the stale tier (`opus → claude-opus-5`); `sonnet`/`fable` and pinned
> IDs pass through unchanged. Repoint or drop a pin per-repo with an additive
> `.loom/config.json` → `sweep.modelAliases` object (no code change):
>
> ```json
> { "sweep": { "modelAliases": { "opus": "claude-opus-5" } } }
> ```

### Complexity tier map (`sweep.tierModels`, issue #4238)

The Curator classifies every issue it curates on one axis — **how expensive is it to be wrong?** — and emits a `<!-- loom:complexity=<tier> -->` marker (see `curator.md`). There are three cost-of-being-wrong strata:

| Tier | Meaning | Recommended Claude model |
|---|---|---|
| `mechanical` | A mistake is obvious just reading the change (file splits, renames, dead-code deletion, constants). | `haiku` |
| `routine` | A mistake would surface in tests or review. Most bug fixes and small features. **Default** (absent marker ⇒ routine). | `sonnet` |
| `complex` | A mistake could pass tests and review unnoticed (architecture, subtle logic, money/security/destructive migrations). | `opus` |

At **Builder** dispatch (precedence tier 2.5, between tiers 2 and 3), the sweep resolves the model for the issue's stratum from `sweep.tierModels[<runtime>][<tier>]` — a **runtime-neutral** map of *logical* tiers (the profile picks a logical tier; `sweep.modelAliases` still does the alias→ID step). Resolution is a command, not a judgement call:

```bash
MODEL="$(./.loom/scripts/resolve-tier-model.sh <issue> <runtime>)"   # exit 3 ⇒ no mapping, fall through to tier 3
```

**No `tierModels` block ships in `defaults/config.json`.** With none configured, `resolve-tier-model.sh` exits 3 and dispatch falls through to the tier-3 role default — so an unconfigured repo's dispatch decisions are **byte-for-byte identical to before this feature**. A workspace opts into cost routing by adding the map (a Codex adapter supplies its own IDs under its own runtime key, per the #4167 adapter contract):

```json
{
  "sweep": {
    "tierModels": {
      "claude": { "mechanical": "haiku", "routine": "sonnet", "complex": "opus" },
      "codex":  { "mechanical": "gpt-5-mini", "routine": "gpt-5", "complex": "gpt-5-codex" }
    }
  }
}
```

Hard bounds (same as the No-Fable invariant): the tier map is **Builder-only**, **never resolves to `fable`** (`resolve-tier-model.sh` refuses and falls through), and tier-1/tier-2 operator pins still win. In model-cost experiment mode the tier-map resolution is suppressed for the forced arm (the marker is still read as the stratification key).

### Optimization profile switch (`sweep.optimization`, issue #4238 Phase B)

`sweep.tierModels` above requires hand-editing a map. `sweep.optimization` is the operator-facing policy switch that picks a **preset** over that same map instead: `.loom/config.json` → `sweep.optimization`: `"cost"` | `"speed"` | `"balanced"` (default `"balanced"`), env override `LOOM_SWEEP_OPTIMIZATION` — the standard **env > config > default** precedence used elsewhere in this repo (`sweep.escalation`, `sweep.max_doctor_cycles`).

```json
{ "sweep": { "optimization": "cost" } }
```

The preset supplies a tier's logical model **only when `sweep.tierModels[<runtime>][<tier>]` has no entry for it** — an explicit `tierModels` entry always wins, so an operator who has hand-tuned part of the map keeps that tuning under any profile. The shipped presets:

| Profile | `mechanical` | `routine` | `complex` | Rationale |
|---|---|---|---|---|
| `balanced` (default) | *(unset)* | *(unset)* | *(unset)* | No preset materialized — dispatch is byte-identical to an unconfigured repo. |
| `cost` | `haiku` | `sonnet` | `opus` | The full 3-stratum spread — cheapest model the Judge gate can safely correct. |
| `speed` | `sonnet` | `opus` | `opus` | Wall-clock in a sweep is dominated by Judge-rejection / Doctor **round-trip count**, not per-turn latency, so `speed` starts a tier higher than `balanced` to buy fewer retries rather than fewer/cheaper tokens per turn. `complex` is already at the ceiling under `balanced`'s own tier-2.5 `complex` bump, so `speed` leaves it unchanged and instead raises `mechanical`/`routine`. |

The profile is expressed in the same runtime-neutral logical tiers (`haiku`/`sonnet`/`opus`) as `sweep.tierModels` and applies uniformly across runtimes — a Codex adapter under the #4167 contract resolves the same logical names to its own IDs, so there is no separate per-runtime preset table. All the tier-map hard bounds above apply identically to the profile: Builder-only, never resolves to `fable`, tier-1/tier-2 pins win, suppressed in model-cost experiment mode. An invalid `sweep.optimization` value (either source) warns and falls back to `balanced` — it never fails dispatch. Implementation: `resolve_optimization_profile` / `optimization_preset` in `loom-daemon/src/script_helpers/model_tiers.rs`, wired into `resolve_tier_model` (`resolve-tier-model.sh`'s native backend, reached via `resolve-model.sh --tier`); see that module's unit tests for the full profile × stratum × precedence matrix.

**Workspace override example** (`.loom/config.json`):

```json
{
  "terminals": [
    {
      "id": "terminal-1",
      "name": "Builder",
      "role": "claude-code-worker",
      "roleConfig": {
        "workerType": "claude",
        "roleFile": "builder.md",
        "model": "claude-sonnet-4-6"
      }
    }
  ]
}
```
