# Runtime Adapter Contract

Loom's worker runtime is the CLI agent tool that actually executes a role
prompt — reads the instructions, drives the tools, edits the code, and exits.
Historically that runtime was hardwired to Claude Code at every layer. This
document is the **normative contract** a new runtime adapter implements against
so that Loom can drive Claude Code, OpenAI Codex CLI, Amp, oh-my-pi (omp), and
future tools through **one** interface instead of a growing pile of per-runtime
special cases.

It is the reference for the multi-runtime effort tracked by epic **#4167**
(first-class multi-runtime worker support) and the fork-harvest triage in
**#4165**. The collaboration model is upstream PRs from the gpeyton/loom fork
(see the [fork mapping table](#fork-mapping-table)), not one-way cherry-picks.

> **Path convention.** This doc lives at `defaults/docs/runtime-adapters.md` in
> the Loom source repo and cites `defaults/` paths throughout. A consumer
> install maps `defaults/docs/` → `.loom/docs/` (NOT `defaults/.loom/docs/`), so
> the installed copy is `.loom/docs/runtime-adapters.md`. When you follow a code
> reference below, read the `defaults/` copy in the Loom source tree — the
> installed `.loom/docs/*.md` are per-file symlinks whose line anchors do not
> resolve via `git show`.

## Tier policy (read this first)

The contract exists to *generalize* Loom, not to demote Claude Code.

- **Claude Code is adapter #1, the default, and tier-1.** Every existing install
  keeps running Claude Code with **zero regression**. When no runtime is
  selected, Loom dispatches Claude Code exactly as it does today.
- **Non-Claude runtimes are tier-2 by default: CI-gated, no operator
  dogfooding.** A tier-2 adapter must pass its CI leg (spawn smoke test, error
  classification, guardrail-parity doc) but is not run against production
  workloads by the operator. It is admitted as "works in CI", not "trusted on
  the operator's own repos".
- **A runtime is promoted to tier-1 only when someone commits to tier-1
  ownership** — ongoing maintenance of its adapter, its parity doc, and its CI
  leg. Tier is a *maintainership* statement, not a capability statement: a
  perfectly capable runtime stays tier-2 until an owner signs up.
- **Tier-3 is "generic passthrough": unverified, and refused for every
  worktree-mutating role by construction.** Tier-2 is still a real bar — a
  spawn smoke test, error classification, and a guardrail-parity document with
  an explicit residual-gap section — which means Loom has exactly two admitted
  runtimes against an ecosystem of many terminal-compatible coding CLIs. Tier-3
  is the bounded, explicitly-lower-trust escape hatch: a single
  `spawn-generic.sh` template (thin per-CLI wrappers instantiate it) that
  satisfies only [contract point 1](#1-spawn) (Spawn) and the
  generic-transient fallthrough half of [contract point 3](#3-error-classification)
  (Error classification) — no custom pattern table, no sandbox mapping, no
  parity doc. See [Tier 3: generic passthrough](#tier-3-generic-passthrough)
  below for the full shape and why skipping the parity doc is safe.

These are operator policy statements recorded on #4167; the contract encodes
them but does not decide them.

### Adapter status

| Runtime | Adapter | Tier | Parity doc | CI leg | Notes |
|---------|---------|------|-----------|--------|-------|
| Claude Code | `defaults/scripts/spawn-claude.sh` | **1** (default) | n/a — Loom's guards *are* the Claude implementation | the whole existing suite | Zero-regression default; no `LOOM_RUNTIME` needed. |
| OpenAI Codex CLI | `defaults/scripts/spawn-codex.sh` | **2** | [`guardrail-parity-codex.md`](guardrail-parity-codex.md) | `codex-adapter-smoke` in `.github/workflows/ci.yml` (mocked; no live calls) | **Shipped** by epic #4167 Phase 2 (#4468), ported from the gpeyton fork. Requires Codex CLI ≥ 0.146.0. Capability manifest `defaults/runtimes/codex.json` declares `worktreeIsolation: partial`, so `check-runtime-capabilities.sh` fails Builder+codex closed while Judge+codex passes. |
| Amp, oh-my-pi (omp), … | — | — | — | — | Not started (tier-2 candidates; still need a parity doc + CI leg). |
| Aider ([aider.chat](https://aider.chat)) | `defaults/scripts/spawn-aider.sh` (thin wrapper over `defaults/scripts/spawn-generic.sh`) | **3** (generic passthrough, unverified) | n/a — tier-3 does not require one | none (no CI leg is required for tier-3; the checker assertion below is a plain `test-*.sh`, not an adapter-admission gate) | **Worked example** for issue #4780 — proves the tier-3 mechanism end-to-end, not a vetted integration. Capability manifest `defaults/runtimes/aider.json` declares every capability `"no"`, including `worktreeIsolation: "no"` EXPLICITLY (not `"partial"`). |

### Tier 3: generic passthrough

A tier-3 adapter exists to try a low-trust CLI against a **read-only**
role — Judge, Curator, Guide, Auditor — without first writing a full
guardrail-parity document. It is deliberately a much thinner contract than
tier-1/tier-2:

- **Implements only contract points 1 and 3, and only partially.** Point 1
  (Spawn) is the full interface — args passthrough, prompt delivery, a
  runtime-missing exit code — but with no logical model-tier mapping, no
  transcript, and no account/usage-pool integration (points 2 and 4 are
  skipped outright). Point 3 (Error classification) is satisfied by the
  **generic-transient fallthrough only**: a tier-3 runtime gets no bespoke
  pattern table in `classify-error.sh`. Passing its name as the
  `classify_error` provider argument is safe by construction — an
  unregistered provider matches no table and resolves through the shared
  engine's exit-code-first ordering and generic transients, exactly like any
  genuinely unknown provider already does today.
- **No guardrail-parity document (point 6) and no CI smoke leg are required.**
  This is not a lowered bar for tier-1/tier-2 — those promotion criteria are
  unchanged (see the issue's "Non-goals") — it is a *different* trust
  boundary that does not need a documented residual gap, because the boundary
  is enforced mechanically instead of by prose:
- **The capability manifest (point 7) is what makes skipping the parity doc
  safe.** A tier-3 runtime's `defaults/runtimes/<name>.json` declares
  `worktreeIsolation: "no"` **explicitly** — not `"partial"`. There is no
  ambiguity to record ("close but gapped"); it is flatly unverified. Because
  Builder and Doctor both declare `runtimeRequirements: ["worktreeIsolation",
  ...]`, `check-runtime-capabilities.sh --role builder --runtime <tier-3-name>`
  fails closed with exit **78** (`EX_CONFIG`) by the exact same mechanism that
  already keeps Codex's `worktreeIsolation: partial` out of Builder — **no
  code change to the checker was needed**: it already treats any value other
  than exactly `"yes"` as unmet, so `"no"` fails closed identically to
  `"partial"`.
- **Read-only admission is per-role, not automatic for the whole "read-only"
  category.** `judge.json` declares `runtimeRequirements: ["mcp"]`, so a
  tier-3 runtime with no MCP support (the honest default — see below) is
  refused for Judge too, for the same reason it is refused for Builder: a
  declared `"no"` is not a declared `"yes"`. `curator.json`, `guide.json`, and
  `auditor.json` declare **no** `runtimeRequirements` today, so they are the
  roles a no-MCP tier-3 runtime is actually admitted for (any runtime is
  trivially compatible with a role that declares no requirements). A tier-3
  runtime that *does* support MCP can declare `mcp: "yes"` and pick up Judge
  too — the manifest is the single source of truth, not a hardcoded
  runtime-vs-role table.

**The shared template.** `defaults/scripts/spawn-generic.sh` is driven
entirely by environment variables so one implementation backs many thin
per-CLI wrappers instead of duplicating the Spawn interface per CLI:

```bash
#!/usr/bin/env bash
LOOM_GENERIC_RUNTIME_NAME="aider" \
LOOM_GENERIC_CLI_BIN="${LOOM_GENERIC_CLI_BIN:-aider}" \
LOOM_GENERIC_PROMPT_FLAG="${LOOM_GENERIC_PROMPT_FLAG:---message}" \
    exec "$(dirname "${BASH_SOURCE[0]}")/spawn-generic.sh" "$@"
```

`spawn-worker.sh` needed **no change** to dispatch a tier-3 runtime: it
already resolves `spawn-<runtime>.sh` by name off disk, so
`LOOM_RUNTIME=aider` (or `runtimes.default: "aider"`) reaches
`spawn-aider.sh` → `spawn-generic.sh` through the exact same seam Codex uses.

**Worked example (issue #4780).** `defaults/scripts/spawn-aider.sh` wires the
[Aider](https://aider.chat) CLI as a tier-3 runtime end-to-end:
`defaults/runtimes/aider.json` declares every capability `"no"`, so
`check-runtime-capabilities.sh --role builder --runtime aider` exits 78 while
`--role curator --runtime aider` exits 0. This is the *mechanism*, not a
roster of new runtimes — onboarding any other specific CLI as tier-3 is a
separate, per-CLI follow-up (see the issue's "Non-goals").

**Promoting a tier-3 runtime to tier-2** means writing the guardrail-parity
document, adding a CI smoke leg, and — critically — re-declaring its
capabilities honestly (`"partial"` where a real, if incomplete, mechanism
exists; `"yes"` only where it is fully proven). Promotion is a manifest edit
plus the missing artifacts, not a rewrite of the adapter shape.

## The seven contract points

Every runtime adapter implements the same seven-point contract. Each point below
is grounded in the concrete Claude Code implementation that serves as the
reference shape — a new `spawn-<runtime>.sh` / adapter must satisfy the same
interface, not necessarily the same internals.

### 1. Spawn

**Contract:** headless prompt execution → exit code + transcript path. Given a
prompt and a set of passthrough CLI args, the adapter launches the runtime in
non-interactive ("headless") mode, lets it run to completion, and reports the
process exit code. The exit code is the primary success/failure signal (see
[error classification](#3-error-classification)); the transcript is the durable
per-session record of what the runtime did and what it cost.

**Reference implementation:** `defaults/scripts/spawn-claude.sh`. It is a thin
token-rotating launcher that:

1. Resolves the canonical repo root (worktree-aware, via
   `git rev-parse --git-common-dir`).
2. Selects a Claude Code OAuth token from the effective `.loom/tokens/` pool
   (per-repo pool, else the shared machine-level pool) via the native
   `loom-daemon tokens select` CLI (issue #4228), exports
   `CLAUDE_CODE_OAUTH_TOKEN`, and exports `LOOM_TOKEN_NAME` so a downstream
   wrapper can mark exactly the right account bad on a usage fault.
3. `exec`s the underlying CLI — `claude` by default, or
   `.loom/scripts/claude-wrapper.sh` when `--use-wrapper` is passed for
   retry/backoff/auth-cache behavior.

The **interface** every `spawn-<runtime>.sh` must satisfy (the shape, not the
Claude-specific internals):

| Facet | Claude Code reference behavior | Adapter obligation |
|-------|--------------------------------|--------------------|
| **Args passthrough** | Unknown args accumulate into `PASSTHROUGH_ARGS` and are forwarded verbatim to the CLI; `--` forwards the remainder. | Forward Loom-supplied and operator-supplied args to the runtime without reinterpreting them. |
| **Prompt delivery** | `-p "…"` / `--prompt` is passed through to `claude -p`. | Deliver the prompt to the runtime's own headless/print-mode flag. |
| **Model tier env** | `LOOM_MODEL` → `claude --model <v>` unless an explicit `--model` is already present (explicit arg wins). See [model mapping](#2-model-mapping). | Map the logical model tier to the runtime's own model-selection flag with the same precedence. |
| **Effort tier env** | `LOOM_EFFORT` → `claude --effort <v>` unless an explicit `--effort` is present. This is a **session-default** effort; the in-session Task tool exposes no per-rung effort knob. | Map to the runtime's reasoning-effort knob if it has one; otherwise omit (no error). |
| **Missing-pool failure** | Exits **78** (`EX_CONFIG`) when neither token pool exists / all tokens are bad, with a message pointing at `loom-daemon tokens bootstrap`. It never silently falls back to keychain. | Fail with a distinct, non-generic exit code on a missing/exhausted credential source rather than silently degrading. |
| **Runtime-missing failure** | Exits `127` when the `claude` CLI is not on `PATH`. | Fail cleanly when the runtime binary is absent. |
| **Observability** | Emits exactly one structured `spawn-claude: model=<v>` line (and an effort line when resolved) to stderr on every spawn, changing no behavior. | Emit an equivalent single structured line so log scrapers can attribute the dispatch. |

The daemon dispatch path (`loom-daemon`'s `spawn_child`) sets
`LOOM_SWEEP_CLAIM_OWNED` and per-sweep log redirection around this script; an
adapter's spawn entry point is invoked the same way, so it must tolerate that
env var being present (Claude's script logs `LOOM_SWEEP_CLAIM_OWNED` on every
spawn for dispatch diagnosability). **An adapter has no Python bridge to
implement:** `LOOM_PACKAGE_PATH` forwarding — the mechanism that let a
dispatched `spawn-claude.sh` locate a Python package — was retired end-to-end in
#4228 once token selection went native, and the package itself was deleted in
epic #4081 Phase 4 (#4557). Nothing on the spawn path reads or sets it.

The runtime-neutral **dispatch seam** that realizes this contract point today —
`spawn-worker.sh` plus the `runtimes` config block — is specified in
[Phase 1: the `spawn-worker.sh` runtime-dispatch seam](#phase-1-the-spawn-workersh-runtime-dispatch-seam)
below.

### 2. Model mapping

**Contract:** map Loom's **logical model tiers** (`opus`, `sonnet`,
`sonnet@xhigh`, `fable`, …) to the **runtime-specific model IDs** the runtime
dispatches on the wire. Loom's roles, the sweep escalation ladder, and the
model-cost experiment all keep naming *logical* tiers; each adapter owns the
single indirection from logical tier to its own concrete IDs.

**Reference implementation:** `defaults/scripts/resolve-model.sh` (a thin stub
delegating to `loom-daemon resolve-model`, implemented in
`loom-daemon/src/script_helpers/model_tiers.rs`). It is the single indirection point that
resolves a logical alias to the concrete ID *before* dispatch. It exists because
a bare alias is not always current on the wire — the CLI's own `opus` alias
still resolves to a previous-generation model, so the shipped map pins the stale
tier (e.g. `opus → claude-opus-5`) while `sonnet`/`fable` and pinned IDs pass
through unchanged. The mapping is repointable per-repo via `.loom/config.json` →
`sweep.modelAliases` with no code change. In `spawn-claude.sh` the resolved tier
arrives as `LOOM_MODEL` and becomes `claude --model <id>` (explicit `--model`
arg wins).

A new adapter provides the equivalent tier→ID table for its runtime (e.g. Codex
logical `opus` → an OpenAI reasoning-model ID). The fork's "cost of being wrong"
tiering is the seed for how a non-Claude runtime should choose which concrete
model a tier maps to.

**The Codex adapter deliberately does NOT do this yet (#4468).** It ships
*model-mapping minimalism*: `LOOM_MODEL` is forwarded verbatim to `codex -m`, an
explicit `-m`/`--model` wins, `LOOM_CODEX_MODEL` supplies an optional static
adapter default, and with none of those set **no `-m` flag is emitted at all** so
the Codex CLI/profile default is preserved (exactly `spawn-claude.sh`'s
no-`--model` behavior). There is no logical-tier→OpenAI-ID table, because Loom's
tier names (`opus`, `sonnet`, `fable`) are Claude names and inventing an
OpenAI-side mapping is precisely the reconciliation flagged below. Reasoning
effort maps to a config override (`-c model_reasoning_effort=<v>`) because
`codex exec` has no `--effort` flag. Full tier resolution for Codex is Phase 4.

**Complexity tier map (`sweep.tierModels`, issue #4238).** The higher-level
`sweep.tierModels[<runtime>][<tier>]` map (Curator marker → logical tier →
model, `mechanical`/`routine`/`complex`) is exactly this per-runtime table. It is
resolved **entirely orchestrator-side** — the `/loom:sweep` skill runs
`./.loom/scripts/resolve-tier-model.sh <issue> <runtime>`, which delegates the
config lookup to `loom-daemon resolve-model --tier` (`resolve_tier_model` in
`loom-daemon/src/script_helpers/model_tiers.rs`) and then the alias→ID step to
`resolve-model.sh`. **The Rust daemon does not
participate**: it never reads the complexity marker — it dispatches with an
explicit/inherited `--model` and forwards nothing else — so there is no daemon
counterpart to keep in lockstep for the tier map, and its `sweep.modelAliases`
resolution (`read_model_aliases` / `resolve_dispatch_model`) is untouched by
#4238. The two-language divergence flagged below is therefore **not widened** by
the tier map; when the adapter contract unifies the alias resolvers, the tier
map layers cleanly on top of whichever single-source resolver wins.

**Optimization profile (`sweep.optimization`, issue #4238 Phase B).** The
`cost`/`speed`/`balanced` policy switch that selects a preset over the tier map
above (see `model-selection.md` "Optimization profile switch") is, for the same
reason, **also orchestrator-side only**: `resolve_optimization_profile` /
`optimization_preset` live in `loom-daemon/src/script_helpers/model_tiers.rs`
alongside `resolve_tier_model`, reached through the same `resolve-tier-model.sh`
call —
there is no separate dispatch path to keep in lockstep. It does not touch
`sweep_registry.rs` for the same reason the tier map does not: the Rust daemon's
`resolve_dispatch_model` only ever resolves `sweep.modelAliases` for its own
`--model` forwarding and has no participation in the Builder-only tier-2.5
resolution chain the profile extends. Verified against `loom-daemon/src/sweep_registry.rs` —
no `tierModels`/`optimization` schema validation exists there to keep in lockstep.

> **Open reconciliation item — do not resolve here.** `sweep.modelAliases` has a
> known **Rust/Python divergence**: the Rust dispatch resolver and the Python
> `model_tiers` resolver do not treat the alias map identically (the Rust side is
> tiered; the Python side is not). A unified model-mapping layer for the adapter
> contract must reconcile these two resolvers — but that reconciliation is
> tracked separately (epic #4167, Phase 4 "pool + tiering integration") and is
> **out of scope for this contract doc**. An adapter author should be aware the
> single-source model map is not yet single-source across both runtimes.

### 3. Error classification

**Contract:** classify a `(output, exit_code)` pair into a small, stable set of
categories so the dispatcher knows whether to rotate the token, retry, escalate,
or fail. The categories drive account-pool health and the sweep's
refusal/rejection handling.

**Reference implementation:** `defaults/scripts/lib/classify-error.sh`. It is
**exit-code-first** (a clean `exit 0` is `SUCCESS` regardless of output content,
the #3233 fix) and only inspects output on a genuine non-zero exit. Its category
set:

| Category | Meaning | Dispatcher action |
|----------|---------|-------------------|
| `SUCCESS` | exit 0 | proceed |
| `TIMEOUT` | exit 124/137 | productive cycle, not a failure |
| `CWD_DELETED` | worktree removed mid-run | abandon cleanly |
| `TOKEN_EXPIRED` | 401 / OAuth expired | skip this token |
| `TOKEN_EXHAUSTED` | quota / weekly / usage limit | rotate to another account, mark bad |
| `SESSION_LIMIT` | concurrent-session cap (healthy account) | re-select, retry, do **not** mark bad |
| `MODEL_REFUSAL` | safety classifier refused the turn | drop one ladder rung, no Doctor cycle consumed |
| `RECOVERABLE` | rate limit / 5xx / network | retry with backoff |
| `FATAL` | non-recoverable **configuration** fault — retrying the identical invocation cannot succeed | fail fast; do not retry, do not rotate |

This file is now a **shared classification engine plus per-provider pattern
tables** (the structure #4190 extracted, seeded by the fork's PR #6): the engine
owns exit-code-first ordering, the category enum, and the generic-transient
fallthrough; each provider table owns only that runtime's failure-signature
regexes. Provider selection is `classify_error <output> <exit_code> [provider]`
with precedence *explicit 3rd arg > `$LOOM_RUNTIME` > `"claude"`*, so two-arg
legacy callers (e.g. `claude-wrapper.sh`) classify bit-identically to before the
split. An unknown provider never errors — it matches no table and resolves
through the generic transients.

Two tables ship today:

- **`claude`** — the reference implementation: `CWD_DELETED` → `MODEL_REFUSAL` →
  `TOKEN_EXPIRED` → `SESSION_LIMIT` → `TOKEN_EXHAUSTED` → the CLI's
  "No messages returned" transient. Order is load-bearing (`SESSION_LIMIT`
  before `TOKEN_EXHAUSTED`, #3947).
- **`codex`** — added with the Codex adapter (#4468): `FATAL` config faults
  (trusted-directory refusal, unknown `-c` config field, unconstructable
  sandbox) → `TOKEN_EXHAUSTED` (plan/quota wording) → `TOKEN_EXPIRED`
  (401 / `codex login` / refresh-token failures). Every pattern was observed on,
  or extracted from, codex-cli 0.146.0 — none is guessed.

Adding a runtime therefore means **contributing a table, never touching the
categories**. Two lessons from the Codex table are worth carrying into the next
adapter:

- **Leave a category unimplemented rather than guessing.** `codex` deliberately
  has no `CWD_DELETED` / `SESSION_LIMIT` / `MODEL_REFUSAL` patterns because that
  runtime has no known wording for them, and reusing Claude's phrasing would
  produce confident nonsense. An unmatched category simply falls through to
  `RECOVERABLE`, which is the correct conservative default.
- **Pick the ordering that makes mis-classification cheap.** `codex` checks
  quota wording *before* 401 wording because `TOKEN_EXPIRED` marks an account bad
  with a reason that persists until manual intervention, while
  `TOKEN_EXHAUSTED`'s TTL-expires on its own.

`FATAL` is no longer purely reserved: the `codex` table is its first producer.
No `claude` input returns `FATAL`, so every pre-existing caller is unaffected.
Callers such as `claude-wrapper.sh` source this file rather than duplicating the
patterns, so the category set must stay stable across runtimes.

### 4. Usage accounting

**Contract:** feed the account pool the session cost/limit signals it needs to
rank and rotate accounts. The pool needs to know, per session: which account was
used, whether it hit a usage/session/weekly limit, and (for cost analysis) the
per-message token usage and model.

**Reference shape:** `spawn-claude.sh` exports `LOOM_TOKEN_NAME` so the failing
account is identified precisely (not guessed from file mtimes); the
`TOKEN_EXHAUSTED` / `SESSION_LIMIT` classifications above tell the pool whether
to mark an account bad or merely re-select. Durable cost recovery comes from the
runtime's per-session **transcript** (Claude Code writes per-message `usage` +
`model` to a JSONL transcript; the #3726 archiver and #3725 harvest read it).

An adapter must expose the equivalent for its runtime: a way to attribute a
session to an account, a limit/exhaustion signal (via the error categories
above), and — for tier-1 cost parity — a transcript or usage stream with
per-turn token counts and the model used. Where the runtime provides no
transcript, cost fidelity degrades to the aggregate log (Loom already tags this
as `token_fidelity: sweep-aggregate-log | none`). The provider-aware account pool
(a per-account `provider` field, provider-aware selection, `CODEX_HOME`-style
profile rotation) is the fork's shipped work (fork PRs #12/#17) and is the
consumer of these signals.

Loom's native account foundation separates lifecycle from policy:
`loom-daemon accounts` securely creates, imports, inspects, disables,
reauthenticates, quarantines, and purges named Codex profiles, while automatic
ranking/rotation remains deferred to #4493. The lifecycle CLI treats
`auth.json` as opaque mutable canonical state, enforces `0700`/`0600`
permissions, and emits only bounded secret-free diagnostics.

### 5. Instruction format

**Contract:** declare which instruction files the runtime reads, and generate
them from a single source so role/repo instructions never fork per-runtime.

- **`AGENTS.md`** is the cross-runtime single source — an Agentic AI Foundation
  (Linux Foundation) standard read natively by Codex, Amp, Cursor, Copilot, Zed,
  Jules, oh-my-pi, and others (see https://agents.md). It is the runtime-neutral
  instruction anchor.
- **`CLAUDE.md`** is Claude Code's richer native format. Claude Code also reads
  `AGENTS.md`, but `CLAUDE.md` carries the full operating surface.

Both derive from one source (#4479, seeded by the fork's `AGENTS.md` codegen,
fork PR #8): `defaults/scripts/generate-agents-md.sh` extracts the
`agents-md:include` marker ranges from `defaults/.loom/CLAUDE.md` and wraps them
with dual-runtime framing, writing the checked-in `defaults/.loom/AGENTS.md`
(CI `scripts/check-agents-md-sync.sh` fails if it is stale). So a new runtime
that reads `AGENTS.md` gets correct instructions with no per-runtime prompt fork,
and there is exactly one place a human edits the prose. An adapter declares its
instruction-file set (e.g. Codex reads `AGENTS.md` + `.codex/` config); it must
**not** introduce a per-runtime copy of the role prompts.

### 6. Permission / sandbox mapping

**Contract:** map Loom's guard-hook *intent* to the runtime's own sandbox
mechanism, and ship a guardrail-parity document with an explicit residual-gap
section. Loom's `PreToolUse` guards (`guard-destructive.sh`,
`guard-loom-workflow.sh`, `guard-worktree-paths.sh`) are Claude-Code-specific —
they are Claude Code hooks. Another runtime has its own sandbox model
(allow/deny command lists, filesystem confinement, network policy), which will
not match Loom's guards one-for-one.

**Shipped example:** [`guardrail-parity-codex.md`](guardrail-parity-codex.md) is
the Codex adapter's parity doc (#4468) and the reference shape for the next one.
Naming convention: `defaults/docs/guardrail-parity-<runtime>.md`.

**Adapter obligation:** every adapter MUST ship a `GUARDRAIL-PARITY.md`-style map
of *Loom guard intent → runtime sandbox mechanism* (e.g. "force-push-to-main
deny → Codex sandbox deny-rule X"), plus an **explicit residual-gap section**
naming the Loom protections the runtime's sandbox does **not** cover. **No
runtime is admitted without this parity doc.** This makes the trust boundary a
documented artifact rather than an assumption — the operator can see exactly what
is and is not enforced before promoting the runtime. The fork's
`GUARDRAIL-PARITY.md` (fork PRs #20/#40) is the seed; the fork's finding that
native Codex agents must be *prohibited* for Loom lifecycles (fork PR #59) is the
kind of hard constraint a parity doc records.

### 7. Capability declaration

**Contract:** each runtime declares the capabilities it supports — MCP,
subagents, hooks, skills, worktree isolation — as yes/no/partial. Dispatch
matches a role's *requirements* against a runtime's *declaration* and refuses a
mismatch up front instead of failing downstream.

This doc specifies only the **schema sketch**; the matcher implementation is a
separate issue (epic #4167, design pillar 2). Sketch:

```jsonc
// A runtime's capability declaration (illustrative shape only)
{
  "runtime": "codex",
  "capabilities": {
    "mcp": "partial",          // yes | no | partial
    "subagents": "no",
    "hooks": "no",
    "skills": "no",
    "worktreeIsolation": "yes"
  }
}
```

Roles declare requirements (e.g. Builder needs `worktreeIsolation` + `mcp`;
Judge needs read-only + forge access). Dispatch computes role → runtime
compatibility and refuses to dispatch a role onto a runtime that cannot meet its
requirements, rather than letting the session fail partway. The declaration is
per-runtime; the requirements are per-role; the match happens at dispatch time.

**Capability contract (#4170, enforced for daemon scheduling by #4494):** the
declaration and requirement sides exist as data and are interpreted identically
by the standalone checker and daemon admission:

- **Declaration** — `defaults/runtimes/<name>.json` (e.g.
  `defaults/runtimes/claude.json`), matching the sketch above exactly (tri-state
  `"yes" | "no" | "partial"` string values, capability set `mcp`, `subagents`,
  `hooks`, `skills`, `worktreeIsolation`).
- **Requirements** — an optional `"runtimeRequirements"` array on a role sidecar
  (`defaults/roles/<name>.json`), e.g. `"runtimeRequirements": ["worktreeIsolation",
  "mcp"]` on `builder.json`. A role with no `runtimeRequirements` key has no
  constraints (any runtime is compatible). This is a distinct field from the
  pre-existing `suggestedWorkerType` (a dispatch *preference* hint) — the checker
  reads only `runtimeRequirements`.
- **Matcher** — `defaults/scripts/check-runtime-capabilities.sh --role <name>
  --runtime <name>` loads both files and checks requirements ⊆ capabilities,
  where a requirement is satisfied only by a declared `"yes"` (`"partial"` AND
  `"no"` both fail closed identically — this is why a tier-3 manifest can
  declare `"no"` explicitly instead of reusing `"partial"`'s "close but
  gapped" connotation, with no change to the matcher itself). Exit 0 on match
  or no-requirements, exit 78 (`EX_CONFIG`) on mismatch naming each unmet
  capability, non-zero with a distinct message on an unknown/missing role or
  runtime file.

**Second manifest (#4468):** `defaults/runtimes/codex.json` declares
`mcp: "yes"`, `subagents: "no"` (the fork PR #59 prohibition), `hooks: "partial"`,
`skills: "partial"`, and `worktreeIsolation: "partial"`. Because `"partial"` fails
closed, `check-runtime-capabilities.sh --role builder --runtime codex` exits 78
while `--role judge --runtime codex` passes — the manifest mechanically encodes
the parity doc's residual gaps, and the CI leg asserts both outcomes. That is the
intended relationship between points 6 and 7: the parity doc states the gap in
prose, the manifest makes it enforceable.

**Third manifest, tier-3 (#4780):** `defaults/runtimes/aider.json` declares
**every** capability `"no"`, including `worktreeIsolation: "no"` **explicitly**
rather than `"partial"` — there is no residual gap to record in prose because
there is no parity doc for tier-3 at all (see
[Tier 3: generic passthrough](#tier-3-generic-passthrough)). The same "any
non-`yes` value fails closed" matcher rule that enforces Codex's `partial`
enforces this `no` identically — `--role builder --runtime aider` and
`--role judge --runtime aider` (judge requires `mcp`, declared `"no"` here)
both exit 78, while `--role curator --runtime aider` exits 0 because
`curator.json` declares no `runtimeRequirements` at all.

**Why `hooks`/`worktreeIsolation` are still `partial` after #4495.** Loom *does*
now wire Codex's `pre_tool_use` event (`defaults/hooks/guard-codex-bridge.sh`,
installed by `defaults/scripts/provision-codex-hooks.sh`), so the earlier "Loom
wires nothing into it" is no longer true. The manifest values are nonetheless
unchanged, deliberately: they are **evidence-gated**, and the remaining evidence
is recorded machine-readably in `codex.json`'s `capabilityGate.pending` and in
prose in [`guardrail-parity-codex.md`](guardrail-parity-codex.md) § "Promotion
gate". Read that block before changing either value. `defaults/roles/doctor.json`
also declares `["worktreeIsolation", "mcp"]` as of #4495 — Doctor mutates a
worktree exactly as Builder does, so it must fail closed for the same reason
instead of slipping through with no constraints.

### Daemon runtime binding and admission

Standalone periodic roles support per-role bindings:

```json
{
  "runtimes": {
    "default": "claude",
    "roles": {
      "curator": "codex",
      "judge": "codex"
    }
  }
}
```

The daemon resolves an explicit dispatch value (where the API offers one), then
`LOOM_RUNTIME_<ROLE>`, `LOOM_RUNTIME`, `runtimes.roles.<role>`,
`runtimes.default`, and finally `claude`. Empty strings are unset. It
canonicalizes the role first and validates the selected adapter, role sidecar,
runtime manifest, and every required capability before starting a child.
`partial`, `no`, missing, malformed, and unknown values fail closed. The
admitted runtime is pinned into the child environment so `spawn-worker.sh`
cannot make a second, divergent choice.

Unknown keys under `runtimes.roles` are a **hard configuration error**, not a
silently ignored entry: `runtimes.roles.not-a-role` (or a typo such as
`buidler`) refuses every admission for that workspace with a diagnostic naming
the offending key, so a misconfigured binding can never leave a role quietly
running on the default runtime. A known key with an **empty** value keeps its
established "unset" meaning and falls through to the next precedence tier.
`loom-daemon validate` also checks `runtimes.roles` proactively (#5006), using
the identical fail-closed rules, so a bad binding is caught any time an
operator runs the command — not only the next time a role's tick actually hits
it.

**Runtime manifest resolution and the bundled fallback (#5002).** The role and
runtime manifest directories are resolved independently (#4688): each prefers
`.loom/roles` / `.loom/runtimes` when the subdirectory exists on disk, and
otherwise falls back to `<repo_root>/defaults/roles` / `defaults/runtimes`.
That `defaults/` fallback only ever resolves on the Loom *source* checkout
itself — a consumer install has `.loom/`, never `defaults/` — so on every
other managed repo it was unreachable by construction. If a consumer repo's
`.loom/runtimes/` is missing entirely, or missing just one runtime's manifest
(e.g. a pre-#4700 install that was never resynced), a non-builtin runtime had
no fallback at all and failed closed even though the daemon binary ships an
adapter for it. The daemon now also carries a **bundled fallback**: manifests
for the runtimes it ships adapters for (`claude`, `codex`, `aider`) are
compiled directly into the binary via `include_str!` from
`defaults/runtimes/*.json` at build time, and consulted whenever the on-disk
manifest lookup misses. An on-disk `.loom/runtimes/<name>.json` — however it
got there — always wins over the bundled copy. Only a runtime the binary was
never built with a manifest for (e.g. an operator-defined custom runtime with
no on-disk manifest either) still fails closed with no fallback at all — and
the diagnostic in that case now names `<repo>/.loom/runtimes/<name>.json`, a
path reachable from a consumer repo, instead of the unreachable
`defaults/runtimes/<name>.json` fallback path.

#### Per-role model override (`autonomous.roleRunner.roleModels`)

`runtimes.roles` gives each standalone role its own **runtime** axis, but the
model the daemon pins per role-runner tick was, before #5001, a single global
value (`autonomous.roleRunner.model`, resolved by `resolve_role_runner_model` in
`loom-daemon/src/role_runner.rs`). The two axes could not disagree, so pointing
one role at a different provider guaranteed a mismatch: with
`LOOM_RUNTIME_JUDGE=codex` set, the globally-pinned Claude alias (`sonnet`) was
forwarded verbatim as `--model sonnet` to the Codex adapter, which rejects it
(`The 'sonnet' model is not supported when using Codex with a ChatGPT account.`,
HTTP 400). The classifier treated that exit as `RECOVERABLE`, so every Judge tick
retried and re-failed indefinitely, fleet-wide, until the env var was reverted.

`autonomous.roleRunner.roleModels` adds the matching **model** axis — a
`{ "<role>": "<model>" }` map whose per-role entry sits one tier **above** the
global `autonomous.roleRunner.model`:

```json
{
  "runtimes": { "roles": { "judge": "codex" } },
  "autonomous": {
    "roleRunner": {
      "model": "sonnet",
      "roleModels": { "judge": "gpt-5-codex" }
    }
  }
}
```

Here Judge runs on Codex with a Codex-valid model while Curator and Champion stay
on Claude with `sonnet`, all from config. The resolution precedence for a given
role is:

**`autonomous.roleRunner.roleModels.<role>` > `autonomous.roleRunner.model` >
`autonomous.model` > shipped `DEFAULT_DISPATCH_MODEL` (`sonnet`)**

Keys are lower-cased and trimmed (so a `"Judge"` key matches the `judge` role the
runner dispatches under); a blank key, or a blank / non-string value, is dropped
so an override never emits `--model ""`; and an absent / malformed / non-object
`roleModels` soft-fails to "no overrides" (every role falls through to the global
chain, zero behavior change). Values resolve through the same `sweep.modelAliases`
tier map every other model tier uses (a per-role `"opus"` still reaches
`claude-opus-5`), and a runtime-specific model ID such as `gpt-5-codex` passes
through unchanged. The per-role log header (`role-<role>.log`) records the
resolved model and names the tier that supplied it
(`source=autonomous.roleRunner.roleModels.<role>`), so an operator can confirm the
pin from the log alone.

#### Model/runtime mismatch refusal (#5028)

`roleModels` above gives an operator a *way* to configure a matching model, but
before #5028 nothing ever verified the two axes actually agreed: the daemon
resolved the model and the runtime independently and forwarded whatever it got.
Set `runtimes.roles.judge = "codex"` with no `roleModels.judge` override, and
the runner still resolved the Claude-shaped default (`sonnet`) and forwarded it
verbatim to the Codex adapter, which 400s — the original #5001 outage,
recurring verbatim for anyone who reaches for the runtime axis without the
matching model one.

`loom-daemon/src/sweep_registry/model.rs` now carries a narrow, fail-open
classifier — `model_family` / `runtime_model_family` / `model_runtime_mismatch`
— that answers exactly one question: are the admitted runtime and the resolved
model **confidently-known, differing** provider families (Claude vs.
OpenAI/Codex)? Only `claude` and `codex` runtimes, and only recognized
Claude/OpenAI model names/aliases (`@effort` suffixes stripped first), are ever
classified; every other runtime (`aider`, a tier-3/custom adapter) or unknown
model name always falls through unrefused. This can only ever catch a launch
that was already guaranteed to fail on the wire — never a launch that might
have worked.

The role runner (`ScriptRoleInvocationRunner::invoke`) now resolves runtime
admission **before** the model — the runtime is a per-role input to the
mismatch check — and if `model_runtime_mismatch` returns `Some(reason)`, the
tick is refused as `RoleTickOutcome::ModelRuntimeMismatch` before any spawn:

- A distinct outcome, deliberately never folded into the generic `Failure`
  tally — same argument as `NoTokenPool` (#4642): this is a permanent config
  conflict detected pre-spawn, not a transient invocation failure.
- Its own `MODEL_RUNTIME_MISMATCH_SKIP_COUNT` counter
  (`model_runtime_mismatch_skip_count()`).
- A one-line operator-facing `detail()` — `"model/runtime mismatch: … (model
  source=…); set autonomous.roleRunner.roleModels.<role> …"` — that
  `record_role_tick` stores on the health ring, which `assess_roles` in
  `health.rs` already renders verbatim: `loom-daemon health` now names the
  broken config key directly, instead of an operator reading a spawn
  transcript to find it (the original #5001 AC2).
- Its own `RootTickLogAction::ModelMismatchEdge` / `ModelMismatchRepeat` pair
  in the multi-workspace loop's per-root log dedup, tracked on a third state
  map fully independent of the `Failure`/`RuntimeRejected` and `NoTokenPool`
  axes — a misconfigured role warns once on the edge and logs at `DEBUG` on
  repeat, never retrying at full WARN-noise cost forever (#5001 AC3). Because
  the check runs before token selection and the CLI start, a misconfigured
  role now costs a config read per tick instead of a token draw plus a full
  session — and it self-heals the moment `roleModels.<role>` is corrected, with
  no restart and no one-shot disable to clear.

`defaults/scripts/spawn-codex.sh` carries an independent copy of the same
refusal for every OTHER caller that reaches this adapter directly with no
daemon preflight in front of it (sweep dispatch also pins models). After the
model-selection block resolves an effective model (explicit `-m`/`--model` >
`LOOM_MODEL` > `LOOM_CODEX_MODEL`), a Claude-shaped value (`opus`, `opusplan`,
`sonnet`, `haiku`, `fable`, or `claude*`, `@effort` stripped) logs the same fix
options and exits `78` (`EX_CONFIG`) before any auth work. Escape hatch:
`LOOM_CODEX_MODEL_CHECK=0`.

Daemon admission runs before any claim lock, forge mutation, account selection,
log header, or child spawn. Successful sweep status and
`sweep.global.dispatch` events include both `runtime` and `runtime_source`.
Refused work is represented too: the daemon publishes
`sweep.global.runtime_rejected` carrying `kind`, `role`, `runtime`,
`runtime_source`, `unmet_capabilities`, and `reason` — emitting it is a pure
publish that creates no claim, account, or log side effect.

Rejected IPC dispatches return `RuntimeRejected` with structured `role`,
`runtime`, `source`, `unmet_capabilities`, and `reason` fields, and **both**
real dispatch clients model it:

- `loom-daemon dispatch <N>` renders the role/runtime/source/unmet diagnostic
  and exits `78` (`EX_CONFIG`) — the same code the shell checker uses for a
  mismatch — so a script can tell a capability refusal apart from a daemon
  error without parsing text.
- The MCP bridge (`mcp__loom__dispatch_sweep`) returns the parsed rejection as
  a structured `runtime_rejection` object plus a rendered summary line, instead
  of an opaque "unexpected response".

These records contain selection metadata only; configuration values,
environment contents, tokens, and credentials are never serialized.

The native admission path and `check-runtime-capabilities.sh` are held to one
decision by a shared conformance matrix over **every** shipped role/runtime
pair, asserting identical decisions *and* identical named unmet-capability
sets. The checker's `--json` flag (additive; exit codes and stderr are
unchanged) emits that comparison surface:

```json
{"role":"builder","runtime":"codex","decision":"reject",
 "unmet":["worktreeIsolation"],"reason":"unmet capabilities: worktreeIsolation"}
```

A `/loom:sweep` is different from a standalone role tick: it is one coordinator
process for the entire Curator → Builder → Judge → Doctor → Merge lifecycle.
Loom does not switch its CLI runtime between those nested phases. Consequently
the daemon resolves the sweep runtime once from the global/default tiers and
admits it against Builder's strongest lifecycle requirements. A per-role
`curator: codex` binding can route a standalone Curator tick to Codex, but it
does not make a full sweep partly Codex. With the shipped manifests, standalone
Curator and Judge can use Codex; Builder and a full sweep cannot, because Codex
declares `worktreeIsolation: partial`.

## Phase 1: the `spawn-worker.sh` runtime-dispatch seam

Contract point 1 ([Spawn](#1-spawn)) is realized today by a concrete
**runtime-dispatch seam** so the underlying runtime is a swappable adapter rather
than a hardwired path. Claude Code is adapter #1; the Codex adapter
(`spawn-codex.sh`, shipped in Phase 2 / #4468) slots in behind the same seam
with no caller change — the seam needed no modification to admit it. This is
**Phase 1** of epic **#4167** and is a **zero-behavior-change** extraction: with
nothing configured, the seam execs the same `spawn-claude.sh` Loom always ran.
(This upstreams the dispatch-seam shape the fork's PR #9 built — see the [fork
mapping table](#fork-mapping-table) — as Loom's own Phase 1 implementation.)

### The dispatcher: `spawn-worker.sh`

`defaults/scripts/spawn-worker.sh` (installed to `.loom/scripts/spawn-worker.sh`)
is a thin dispatcher that resolves a runtime name and execs the matching
`spawn-<runtime>.sh` runner in the same directory, forwarding every argument
verbatim. Because it uses `exec`, the runner's exit code is the dispatcher's exit
code — so the [error classification](#3-error-classification) contract is
unaffected by the extra hop.

```bash
.loom/scripts/spawn-worker.sh -p "your prompt"
LOOM_RUNTIME=claude .loom/scripts/spawn-worker.sh --use-wrapper -p "..."
```

Callers migrate from `spawn-claude.sh` to `spawn-worker.sh` to gain runtime
selection; direct `spawn-claude.sh` callers remain supported. The daemon
`SweepRegistry` uses this dispatcher and sends generic `--effort`,
`--dangerously-skip-permissions`, and `--use-wrapper` conventions. Each adapter
must consume or translate these options and must not pass options unsupported
by its CLI.

The machine-level `loom sweep <N>` dispatcher (`scripts/loom`'s `loom_cmd_sweep`)
is one such migrated caller (#4480): it resolves the repo's `spawn-worker.sh` and
execs `spawn-worker.sh -p "/loom:sweep <N>" --dangerously-skip-permissions`
instead of `claude` directly, so a Codex-hosted (or any non-Claude) operator can
dispatch the canonical single-issue lifecycle. With nothing configured this stays
byte-for-byte the old `claude -p ... --dangerously-skip-permissions` invocation
(via `spawn-worker.sh` → `spawn-claude.sh`).

### Adapter observability markers

Daemon-compatible runners emit a small, secret-free contract on stderr:

```text
# LOOM_RUNTIME_RESOLVED runtime=<runtime>
# LOOM_ACCOUNT name=<account-or-profile>   # optional
# LOOM_CLI_START runtime=<runtime>
```

The dispatcher emits runtime resolution, an adapter emits account/profile
selection when available, and the runner emits CLI-start immediately before
launching the runtime CLI. Account values are display names only; credential
values and credential paths must never appear. Parsers anchor these markers
after the newest daemon dispatch header. Historical `spawn-claude:` preamble
and `# CLAUDE_CLI_START` records remain supported.

### Runtime resolution (precedence)

The runtime is resolved with the standard Loom precedence chain
(**env > config > default**):

| Precedence | Source | Notes |
|-----------|--------|-------|
| 1 (highest) | `LOOM_RUNTIME` env var | A non-empty value wins. An **empty** value is treated as unset and falls through. |
| 2 | `.loom/config.json` → `runtimes.default` | Read via the shared config-resolver (soft-fails silently). |
| 3 (default) | built-in `"claude"` | Applies when neither of the above resolves. |

The config read tolerates a missing config file, a missing `runtimes` block, and
a missing `jq` — all of these degrade silently to the built-in `claude` default,
so a bare install with no `runtimes` config sees no behavior change.

### The `runtimes` config block

Add to `.loom/config.json`:

```json
{
  "runtimes": {
    "default": "claude"
  }
}
```

`runtimes.default` names the runtime used when `LOOM_RUNTIME` is unset. The value
must have a matching `spawn-<value>.sh` runner on disk (e.g. `"claude"` →
`spawn-claude.sh`).

### Adding a runtime adapter

Drop a `spawn-<runtime>.sh` runner next to `spawn-claude.sh` (same directory,
executable). It must satisfy the [Spawn interface](#1-spawn) above — accept the
same passthrough-args contract and `exec` its underlying CLI. Then select it
per-run with `LOOM_RUNTIME=<runtime>` or repo-wide with `runtimes.default`.

`spawn-codex.sh` is the worked example. Use it as the shape for a third adapter,
and note the four things it had to do that the bare Spawn table does not spell
out — each of which is likely to recur:

1. **Translate Loom's flag conventions, do not forward them.** `-p`/`--prompt`
   and `--dangerously-skip-permissions` are *Loom* conventions. On `codex exec`,
   `-p` means `--profile`, so forwarding it would have silently selected a
   config profile instead of passing a prompt. Consume Loom's flags, re-emit the
   runtime's.
2. **Own your own scheduling priority.** `spawn-worker.sh` deliberately applies
   no `nice`/`taskpolicy` (issue #4233) — it only `exec`s, so a runner-level
   re-exec covers the whole tree with no double-apply. Copy `spawn-claude.sh`'s
   `LOOM_SWEEP_NICED` block into the new runner.
3. **Check which stream your runtime's metadata is on.** Codex writes the agent's
   final message to stdout and *everything else* — including the `session id:`
   line that is the transcript join key — to stderr. Reporting it therefore
   requires running the CLI as a child with stderr tee'd (recovering the exit
   code from `PIPESTATUS`) rather than a plain `exec`. Verify empirically; do not
   assume.
4. **Neutralize stdin.** A dispatched worker's stdin is a pipe nobody writes to.
   `codex exec` reads non-TTY stdin and appends it to the prompt, so the child
   would block forever; the adapter redirects `</dev/null`. Any runtime with a
   "read the prompt from stdin" mode needs the same treatment.

Two contract points gate admission, and neither is optional:
a **guardrail-parity document** (point 6 — see
[`guardrail-parity-codex.md`](guardrail-parity-codex.md) for the required shape,
including an explicit residual-gap section) and a **CI leg** proving the adapter
spawns correctly against a mocked CLI with no live API calls (the tier-2 gate;
`codex-adapter-smoke` in `.github/workflows/ci.yml` is the model). Add a
capability manifest at `defaults/runtimes/<runtime>.json` (point 7) at the same
time — declaring a capability `"partial"` fails role matching closed, which is
how Codex is correctly kept out of Builder dispatch.

**Want to skip both of those for now?** That is exactly what
[tier-3 generic passthrough](#tier-3-generic-passthrough) is for: instantiate
`spawn-generic.sh` instead of writing a full adapter, declare every capability
`"no"` (including `worktreeIsolation: "no"`, explicitly) in the manifest, and
the runtime is refused for Builder/Doctor with no parity doc required. Use it
to try a CLI against a read-only role first; write the tier-2 artifacts only
once you are ready to trust it more broadly.

### Unknown-runtime failure (exit 78)

If the resolved runtime has no matching `spawn-<runtime>.sh` runner, the
dispatcher exits **78** (`EX_CONFIG`) — the same distinct config-error code the
Spawn contract's *missing-pool* facet uses — with an actionable message naming:

- the resolved runtime,
- where it was resolved from (env vs config vs default), and
- the `spawn-*.sh` runners actually present on disk.

```text
ERROR Unknown runtime 'amp' (resolved from config (runtimes.default)):
ERROR no runner found at /…/.loom/scripts/spawn-amp.sh.
ERROR Available runtimes on disk: claude codex.
```

(The example named `codex` before Phase 2 shipped `spawn-codex.sh`; `codex` is
now a *known* runtime. A regression test in
`defaults/scripts/tests/test-spawn-codex.sh` asserts that `codex` moving from
unknown to known did not weaken this guard for other names.)

### Scope (Phase 1)

- No capability-matrix enforcement in the dispatcher
  ([contract point 7](#7-capability-declaration) is a separate issue) — the seam
  only routes; it does not validate a runtime's feature set.
- `spawn-claude.sh`, `claude-wrapper.sh`, and the Rust `loom-daemon` callers are
  unchanged; migrating callers onto `spawn-worker.sh` is a follow-up once the
  seam has soaked. (The Python `loom-tools` callers this list used to name no
  longer exist — epic #4081 Phase 4, #4557.)

## Fork mapping table

The gpeyton/loom fork already built much of this as parallel special-casing. The
adapter contract is the interface that work slots into as **upstream PRs from the
fork** (not cherry-picks). This is the "your work slots in here" map for the
collaboration:

| Fork PR | What it built | Contract slot | Upstream status |
|---------|---------------|---------------|-----------------|
| #9 | `spawn-worker.sh` spawn dispatcher | **1. Spawn** — the runtime-neutral dispatch entry point | **landed** (Phase 1) |
| #6 | Restructured `classify-error.sh` into per-provider pattern tables | **3. Error classification** — the per-runtime pattern-table shape | **landed** (#4190) |
| #15 | Codex runner | **1. Spawn** — Codex's `spawn-<runtime>.sh` implementation | **landed** as `defaults/scripts/spawn-codex.sh` (#4468). Ported, not cherry-picked: token-pool auth deferred to Phase 4, `--full-auto`/`-a` replaced (absent on `codex exec` 0.146.0), and the skip-permissions → sandbox mapping deliberately diverges (see the parity doc). |
| #16 | `.codex/` config | **5. Instruction format** — Codex's config/instruction file set | not started (separate issue) |
| #20, #40 | `GUARDRAIL-PARITY.md` guardrail parity | **6. Permission / sandbox mapping** — the parity-doc requirement | **landed** as [`guardrail-parity-codex.md`](guardrail-parity-codex.md) (#4468), re-verified against 0.146.0 — several fork claims no longer hold and are corrected there |
| #8 | `AGENTS.md` codegen | **5. Instruction format** — single-source instruction generation | **landed** (#4479) as a *generator* (`defaults/scripts/generate-agents-md.sh`) that extracts `agents-md:include` ranges from `defaults/.loom/CLAUDE.md` — not the fork's hand-authored static file (that would violate this repo's single-source non-goal). CI `check-agents-md-sync.sh` keeps the checked-in `defaults/.loom/AGENTS.md` in sync; scaffolding installs the root pointer + `.loom/AGENTS.md` full guide. |
| #12, #17 | Provider-aware account pool (per-account provider, waterfall fill, `CODEX_HOME` rotation) | **4. Usage accounting** — provider-aware selection consuming the pool signals | Phase 4. #4468 ships only single-profile `CODEX_HOME` passthrough — no pool, no rotation, no bad-token marking. |
| #14 | Reusable CI role workflow (`loom-role.yml`) parameterized by runtime | Cross-cutting — the tier-2 CI gate every non-Claude adapter must pass | partially landed: Codex has its own `codex-adapter-smoke` leg in `ci.yml` (#4468); the parameterized reusable workflow is not adopted |
| #59 | Finding: native Codex agents prohibited for Loom lifecycles | **6/7. Constraint** — encoded in the parity doc + capability matrix | **landed** — recorded as residual gap 9 in the parity doc; `codex.json` declares `subagents: "no"` |

## Non-goals

- **No change to the forge/label state machine.** Runtime choice is invisible to
  the coordination layer — `loom:issue` → `loom:building` → `loom:pr` → merged is
  identical regardless of which runtime a worker uses.
- **No per-runtime role prompt forks.** Instruction content stays single-source
  (contract point 5).
- **No new labels.**

## Related

- Epic **#4167** — first-class multi-runtime worker support (the seven contract
  points' authoritative framing, the phasing, and the fork PR list).
- **#4165** — fork divergence triage (harvest tracking).
- [`guardrail-parity-codex.md`](guardrail-parity-codex.md) — the Codex adapter's
  guardrail-parity doc (contract point 6), including the `CODEX_HOME` profile
  layout / refresh / security-posture reference absorbed from #4469.
- **#4468** — Codex adapter port (Phase 2). **#4470** — Codex canary runs.
- **#4780** — tier-3 "generic passthrough" mechanism: `spawn-generic.sh`, the
  `defaults/runtimes/aider.json` worked example, and the capability-manifest
  gate this section documents. Adapted from a survey of
  [stablyai/orca](https://github.com/stablyai/orca)
  ([survey-orca-2026-07-31.md](https://github.com/rjwalters/loom/blob/main/.loom/docs/survey-orca-2026-07-31.md),
  idea 5), filed from #4775.
- [ADR-0012: Multi-Runtime Worker Support via a Runtime Adapter Contract](https://github.com/rjwalters/loom/blob/main/docs/adr/0012-runtime-adapter-contract.md).
- Fork: https://github.com/gpeyton/loom · `AGENTS.md` standard: https://agents.md
