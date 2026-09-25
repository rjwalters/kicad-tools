# Private-session control bundle (`loom-private-control-v1`)

A private Codex session (`loom-daemon accounts session start NAME
--private-clone HTTPS_URL`, see
[private-session-dispatch.md](private-session-dispatch.md)) owns a writable
clone at `/workspace/repo` and a read-write account profile at
`/home/loom/.codex-profile`. Before this boundary existed, **every guard input
lived in one of those two writable places**, and two disposable-fixture probes
against the production bridge showed what that costs (issue #8839):

1. Writing `{"guards":{"forceScope":"off"}}` into the clone's own
   `.loom/config.json` turned a denied `git push --force origin HEAD:main`
   into an allowed one, and the force-push then rewrote the disposable
   remote's `main`.
2. Deleting `defaults/hooks/guard-codex-bridge.sh` from the clone (the file the
   registered hook named) removed enforcement outright.

Neither probe involved a model call, a real account profile, credentials, or a
production remote — they were fixtures, not a production exploit. They are
nonetheless the exact reason mutable admission stays refused: **#4496 remains
the live production gate and this document does not move it.**

## What the boundary is

Three properties, each *enforced* rather than configured. All of it is in Rust
(`loom-daemon/src/tokens_pool/private_workspace/bundle.rs`); the guards
themselves are unchanged.

### 1. Guard code is image-owned

`docker/session/Dockerfile` stages every guard the bridge executes, every
library those guards source, and the hook provisioner into
`/opt/loom/private-control/` — root-owned, `chmod a-w`, on the session
container's read-only rootfs, under `--cap-drop ALL --security-opt
no-new-privileges` as uid 1000. The managed Codex registration names
`/opt/loom/private-control/hooks/guard-codex-bridge.sh`, so the copies in the
worker's own clone are not on the enforcement path at all.

`guard-destructive.sh` in the bundle is deliberately a **copy of
`guard-destructive-generic.sh`**, not the dispatcher: the dispatcher prefers
`$LOOM_PROJECT_ROOT/.claude/skills/repo/hooks/guard-destructive.sh`, which
inside a private session is a worker-writable path. `seal-control` refuses to
build an image whose two digests diverge.

The **provisioner** is sealed for the same reason: it both writes and checks
the readiness evidence, so running the clone's copy would let a worker certify
its own readiness by replacing one tracked file.

### 2. Effective policy is forced through a channel the worker cannot reach

Every guard category toggle has a documented `LOOM_*` environment override that
wins over `.loom/config.json` and every higher config tier (see
[guard-hooks.md](guard-hooks.md)). The host sets all of them on the **Codex
process itself**, through `docker exec --env`; the guards are children of that
process and inherit them. A worker can still write `.loom/config.json`, invent
a deeper config root, or add a new tier, and change nothing about the decision.

| Forced override | Value | Relation to the shipped default |
|---|---|---|
| `LOOM_FORCE_SCOPE` | `protected` | The Loom-recommended value for autonomous repos (`guard-hooks.md` → "Force-Op Branch Scope Guard"), and what this repo already runs. Denies force ops on protected refs headlessly; leaves an agent's own branches workable. |
| `LOOM_RM_SCOPE` | `repo` | Same as the default. |
| `LOOM_GUARD_SQL` | `1` | Forces the category **on** even where a repo opted out. |
| `LOOM_GUARD_CLOUD` | `1` | Default. |
| `LOOM_GUARD_CARGO_CLEAN` | `1` | Default. |
| `LOOM_GUARD_STASH_SCOPE` | `1` | Default. |
| `LOOM_GUARD_WORKTREE_ISOLATION` | `1` | Default. |
| `LOOM_GUARD_INSTALLED_FILE_WRITES` | `1` | Default. |
| `LOOM_GUARD_WORKSPACE_REGISTRY` | `1` | Default. |
| `LOOM_GUARD_READONLY_FASTPATH` | `0` | Disables an **allow-path** whose `readOnlyFastPathExtra` list is a per-command-word bypass a worker could extend from its own clone. Costs forks, never decisions. |

`guards.reversibleGh` is deliberately absent: it is an opt-in ask that is off by
default, and forcing it on would convert trivially reversible forge operations
into headless denials without protecting anything.

**This set is part of the protocol, not per-repo configuration.** Changing any
value is a `CONTROL_VERSION` bump, because an already-bound session identity
covers the policy map.

### 3. Identity is bound and rechecked

`loom-daemon private-workspace seal-control` writes
`/opt/loom/private-control/manifest.json` at image build time — protocol,
control version, per-asset SHA-256, the forced policy map, the exact
registration command, the bridge's Codex schema pin, and the Codex CLI version
**observed on PATH in the image being built** (never a build argument).

`loom-daemon private-workspace control` re-derives all of it inside the
container, additionally proving the bundle is not writable (it attempts a real
write in every directory holding a sealed asset) and that the managed
registration and Loom's readiness receipt still name the image-owned bridge. It
hashes that observation, the profile's control files, and the manifest into one
64-hex **control identity**.

That identity is checked three times:

| When | Where | What a mismatch does |
|---|---|---|
| Admission (`session start`, `session job`, sweep/role preparation) | Host | Refuses before any mutable work; the session and its volume are preserved untouched. |
| Immediately before spawn, on the exact container and lease being launched | Host (`transport.rs`) | Refuses the launch. |
| Immediately before `exec`ing the model | In-container (`worker_setup::execute`) | Refuses the exec. |

The identity is stored in the account job lease next to the container ID, so a
replaced container, a replaced image, a stale control version, an altered
policy or registration, and a lease that carries no identity at all are each
refused *before* mutable work rather than detected afterwards. A lease written
before this protocol deserializes with an empty identity, which every recheck
refuses rather than treating as proven.

## Supported combinations

| Component | Supported value | How it is established |
|---|---|---|
| Control protocol | `loom-private-control-v1`, `control_version` **1** | Exact-match on both sides; anything else is `Unsupported`. |
| Session image | `ghcr.io/rjwalters/loom-worker-session:<version>` built from `docker/session/Dockerfile` at or after this change | The bundle must be present, sealed, digest-intact and non-writable. |
| Codex CLI | `0.149.1` as pinned by `CODEX_VERSION`; floor `0.146.0` | `seal-control` records `codex --version` as observed in the image and refuses to seal below the floor. |
| Codex hook schema | `pre_tool_use`, pinned at `0.146.0` in `guard-codex-bridge.sh` | Evidence below. |
| Managed hook version | `LOOM_HOOK_VERSION` = 1 in `provision-codex-hooks.sh` | Cross-checked against the receipt at every observation. |

### Codex 0.149.1 vs the 0.146.0 schema pin — evidence, not assumption

The image pins Codex **0.149.1** while `guard-codex-bridge.sh` and
`provision-codex-hooks.sh` pin their tested schema at **0.146.0**. "0.149.1 ≥
the 0.146.0 floor" is not by itself evidence of compatibility, so the real
`@openai/codex@0.149.1` package was examined directly (2026-09-24, Linux x64
`vendor/x86_64-unknown-linux-musl/bin/codex`):

- The binary still embeds the `pre-tool-use.command.input` /
  `pre-tool-use.command.output` JSON schemas, with `hookEventName: "PreToolUse"`
  and the `permissionDecision` / `permissionDecisionReason` fields the bridge
  emits.
- It carries **all seven** of the refusal strings the bridge's 0.146.0 analysis
  documents — `unsupported permissionDecision:allow`, `:ask`, `unsupported
  decision:approve`, `unsupported continue:false`, `unsupported stopReason`,
  `unsupported suppressOutput`, and `permissionDecision:deny without a
  non-empty permissionDecisionReason` — plus three further ones (`reason
  without decision`, `updatedInput without permissionDecision:allow`,
  `permissionDecisionReason without permissionDecision`). The bridge's only two
  outputs are "emit nothing, exit 0" (allow) and a deny carrying a non-empty
  `permissionDecisionReason` and nothing else, so **none of the ten is
  reachable from it.** The 0.146.0 "deny-or-silence" wire remains exactly
  right on 0.149.1.
- Hook trust is still `hooks.state."<identity>".trusted_hash` in
  `config.toml`, and `--dangerously-bypass-hook-trust` is still the only
  non-interactive waiver. There is still no `codex hooks` subcommand, and
  `codex doctor --json` (20 checks on 0.149.1) still reports no hook check. So
  #5005's finding — trust is operator-attested, per profile, once — holds
  unchanged at 0.149.1, and Loom still neither fabricates nor bypasses it.
- **0.149.1 does not validate `hooks.json` eagerly.** A malformed or absent
  registration produces no startup error, no warning, and no `doctor` finding;
  the CLI simply runs unhooked. This is why readiness has to be proven by Loom
  *before* dispatch rather than inferred from the CLI starting successfully,
  and it is the reason the registration and receipt are part of the bound
  identity.

`docker/session/test-image.sh` re-asserts the version pin, the sealed manifest,
and the bridge's wire shape against the **real CLI in the shipped image** on
every image build, so a Codex bump that changes any of this fails the image
smoke test rather than silently shipping.

## Remaining limitations

- **The hook-invocation step itself is not proven credential-free.** Getting the
  real CLI to *invoke* a `pre_tool_use` hook requires a model turn, which
  requires credentials; the tests therefore prove (a) the shipped CLI's own
  embedded wire contract accepts exactly what the bridge emits, (b) the
  registration the CLI would read names the image-owned bridge, and (c) the
  bridge denies the demonstrated escalation when driven with a real
  `pre_tool_use` event. They do not prove the engine dispatched it. Codex
  capability stays `hooks: partial` / `worktreeIsolation: partial`
  ([guardrail-parity-codex.md](guardrail-parity-codex.md)) for this reason,
  among others.
- **Hook trust remains operator-attested per profile** (#5005). The
  registration now names a stable image-owned path, so the operator's one-time
  trust step must be taken against that path.
- **Mid-session removal of `$CODEX_HOME/hooks.json` is not blocked by the
  kernel.** It is blocked by the forced policy (`LOOM_RM_SCOPE=repo` plus Bash
  write-confinement both deny writes outside the clone), it is re-proven
  immediately before the model is exec'd, and it makes the **next** admission
  refuse — but a mount-level immutability for the registration and the profile's
  trust state is a further hardening step, not something this version claims.
- **`config.toml` stays worker-writable** so the canonical authentication
  refresh lifecycle keeps working. Its digest is part of the bound identity, so
  a change is refused at the next recheck, and a private session audits it for
  MCP/plugin/alternate-profile routes before every launch
  (`private_workspace::control`).
- **No fleet enablement.** This is a prerequisite for #8787; #4496 remains the
  live production gate, and the shipped Codex manifest still refuses mutable
  lifecycle roles.

## Authentication is untouched

`auth.json` stays writable in the canonical profile bind mount (0700 profile /
0600 auth, uid 1000), no credential byte enters the bundle, the manifest, the
clone, exported metadata, host logs or an image layer, and no profile
permission is loosened. The integration test replaces `auth.json` through the
canonical write-temp-then-rename path from inside the session and asserts the
host-side file changed atomically.

## Rollback

The boundary is image-resident and versioned, so rollback is an image choice,
not a code revert:

1. **Roll the image back.** `loom-daemon accounts session stop NAME`, then
   `session start NAME --private-clone URL --image <older-tag>`. A host running
   this daemon will then refuse that session with *"ships no
   `loom-private-control-v1` control bundle"* — which is the intended
   fail-closed behavior, not a regression. Private sessions are opt-in and
   default-off; the supported rollback for a bad bundle is to **stop using
   private sessions** until a good image exists.
2. **Roll the daemon back.** A daemon from before this change ignores the
   bundle entirely and behaves exactly as it did before, including on an image
   that ships one. Leases written by the newer daemon carry one extra
   `control` field, which the older daemon ignores.
3. **Never** work around a refusal by deleting the account volume, resetting a
   branch, loosening profile permissions, chmod-ing the bundle writable, or
   passing `--dangerously-bypass-hook-trust`. Every refusal preserves the
   session, the volume and the profile for inspection.

Host sessions, shared-mount sessions, the global `hooks` configuration and
`worktreeIsolation: partial` are all unchanged by this boundary: it applies only
to a private-clone session whose image ships the bundle.
