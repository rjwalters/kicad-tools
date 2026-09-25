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

Four properties, each *enforced* rather than configured. All of it is in Rust
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

### 3. The hook registration and the profile's trust state are immutable from inside (control v2)

Guard *code* being image-owned is not enough on its own, because the file that
tells Codex to run it — `$CODEX_HOME/hooks.json` — lives in the account
profile, which has to stay writable for authentication refresh. Control v1
could only *detect* a change to it, at the next recheck. That leaves a real
window: a worker removes the registration after the pre-exec recheck, every
remaining turn of that session runs unhooked, and nothing re-reads the file
until the next admission — long after the unhooked turns happened.

v2 closes it with **mount topology rather than file modes**. The host binds
each of the three control files **read-only over its own canonical path**:

| File | What it carries |
|---|---|
| `hooks.json` | the managed `PreToolUse` registration naming the image-owned bridge |
| `config.toml` | Codex's own `hooks.state."<id>".trusted_hash` trust state |
| `loom-codex-hooks.json` | Loom's readiness receipt pinning that registration |

Because each path is now a **mount point**, the kernel refuses every route a
worker has to a file in a directory it owns:

| Attempt | Result | Why |
|---|---|---|
| `open(O_WRONLY)` / truncate / `chmod` | `EROFS` | the bind carries `ro` |
| `unlink` | `EBUSY` | the path is a mount point |
| `rename` (away, or over it) | `EBUSY` | ditto, for source and destination |
| `ln -sf` / `ln -f` over it | `EEXIST` | the mount point cannot be replaced |

File modes alone cannot achieve this: the profile *directory* must stay
writable so the canonical atomic `auth.json` refresh (write a sibling temp,
rename over) keeps working, and directory write permission is exactly what
governs `unlink` and `rename`. A mount point is immune to both while leaving
every non-mounted sibling — `auth.json` included — completely untouched.

Both halves are enforced independently, so neither side has to be believed:

- **Host**: `docker::validate_settings` checks Docker's own mount inventory on
  every admission — each control file present, `Type: bind`, `RW: false`, and
  sourced from the identically-named file in the canonical profile. The mount
  count is exact, so a *missing* protection is refused as loudly as a wrong one.
- **Container**: `ProfileImmutability::observe` requires, for every control
  file, both an entry in the kernel's own `/proc/self/mountinfo` naming that
  exact path with `ro`, **and** a real open-for-append that actually fails. A
  boundary that cannot prove both reports `profile-mutable`, binds no identity,
  and is refused.

**Provisioning therefore moves to the host, before the session exists.**
`loom-daemon accounts session start` runs the image's own sealed provisioner in
a throwaway container carrying nothing but the account profile — no workspace
volume, no forge configuration, no network — and only then creates the session
with the read-only binds. A profile that is already correctly provisioned is
left byte-for-byte alone. Inside a session, `private-workspace setup` skips
hook provisioning entirely once it measures the controls as protected: writing
them there is `EROFS` by construction, and whether the frozen registration is
the *right* one is decided by `private-workspace control`, which the host
consults immediately afterwards and refuses admission on.

### 4. Identity is bound and rechecked

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
policy or registration, an unprotected mount topology, and a lease that carries
no identity at all are each refused *before* mutable work rather than detected
afterwards. A lease written before this protocol deserializes with an empty
identity, which every recheck refuses rather than treating as proven.

## Supported combinations

| Component | Supported value | How it is established |
|---|---|---|
| Control protocol | `loom-private-control-v1`, `control_version` **2** | Exact-match on both sides; anything else is `Unsupported`. A v1 image reports v1 and is refused: its in-container half cannot make the control-file claim at all. |
| Session image | `ghcr.io/rjwalters/loom-worker-session:<version>` built from `docker/session/Dockerfile` at or after this change | The bundle must be present, sealed, digest-intact and non-writable. |
| Session mount topology | profile bound read-write; `hooks.json`, `config.toml` and `loom-codex-hooks.json` each bound read-only over their own path | Cross-checked against Docker's mount inventory on the host and `/proc/self/mountinfo` plus a real write probe in the container. |
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
- **0.149.1 does not validate `hooks.json` eagerly, and an untrusted hook fails
  open.** A malformed or absent registration produces no startup error, no
  warning, and no `doctor` finding; the CLI simply runs unhooked. The same is
  true of a *present, correct, untrusted* registration — the control turn below
  drives a real `codex exec` past one and the force-push lands, with no hook
  invocation and no diagnostic of any kind. This is why readiness has to be
  proven by Loom *before* dispatch rather than inferred from the CLI starting
  successfully, and it is the reason the registration and receipt are part of
  the bound identity.

`docker/session/test-image.sh` re-asserts the version pin, the sealed manifest,
and the bridge's wire shape against the **real CLI in the shipped image** on
every image build, so a Codex bump that changes any of this fails the image
smoke test rather than silently shipping.

### The engine really does dispatch to the hook — measured, not inferred

Everything above establishes that the bridge *would* deny and that the
registration the CLI *would* read names the image-owned copy. Neither shows the
engine dispatching to it, and on this CLI that gap is not academic: an
unregistered or untrusted hook fails **open**, silently. §12 of
`docker/session/test-image.sh` closes it by running the real `codex exec`
against a **scripted loopback model provider** — a ~30-line Node server on
`127.0.0.1` in a `--network none`, `--read-only`, `--cap-drop ALL` container as
uid 1000, with the profile's three control files bound read-only exactly as a
session binds them, a local bare repo as the "remote", and no credential
anywhere. The provider emits one `exec_command` call and then a final message,
so the turn is deterministic and offline.

**Hook trust is real, not waived.** Codex persists it only through an
interactive TUI decision, so §12 makes that decision the way an operator does:
it runs the shipped TUI under `tmux` in a throwaway container with the profile
still writable — the same pre-session step
`provision-codex-hooks.sh` tells the operator to take — waits for the
"Hooks can run outside the sandbox" prompt by its text, and answers it. Nothing
writes a `trusted_hash` by hand and `--dangerously-bypass-hook-trust` is passed
nowhere, in this script or in shipped Loom code. What §12 measures is therefore
the production path exactly as it runs.

Two profiles are provisioned and registered identically and differ **only** in
the answer to that one prompt — `2` (*Trust all and continue*) versus `3`
(*Continue without trusting*), asserted by the presence and absence of
`hooks.state…trusted_hash` in their `config.toml`. The identical probe then runs
against each:

| Tool call | Hook trust | Asserted outcome |
|---|---|---|
| `git push --force origin HEAD:main` | **real** | `Command blocked by PreToolUse hook` carrying the guard's own reason; the bare repo's `main` is **unmoved** |
| `printf allowed > …/allowed.txt` | **real** | runs to completion — the hook is consulted and *allows*, so the block above is a decision, not a blanket refusal |
| `git push --force origin HEAD:main` | **none** | no hook event is recorded at all and the force-push **lands**, moving `main` |

The third row is the control that makes the first non-vacuous, and it is also
the executable form of the fail-open finding above: with no persisted trust the
CLI runs unhooked and says nothing about it. Because the two profiles differ in
nothing else, the difference in outcome is attributable to hook trust alone.

A second, always-allow *recorder* hook registered beside Loom's (before the
trust step, so "trust all" covers it too) captures the payload the engine
delivers, and the recorded shape is asserted:

| Field | 0.146.0 schema the bridge documents | **What 0.149.1 actually emits** |
|---|---|---|
| `tool_name` | `shell` | `Bash` |
| `tool_input.command` | argv array `["bash","-lc",…]` | a **string** |

`guard-codex-bridge.sh` already classifies `Bash` and already extracts a string
command, so no bridge change was needed — but until this assertion existed,
every fixture exercised a shape the shipped CLI does not produce.
`loom-daemon/tests/private_workspace_docker` now drives **both** shapes through
the image-owned bridge for the same escalation, so the synthetic layer can no
longer diverge from the engine's own payload unnoticed. A Codex release that
renames the tool or restructures `tool_input` fails the image smoke test.

The turns also prove a non-regression nothing else covers: the real CLI runs
normally with `hooks.json`, `config.toml` and `loom-codex-hooks.json` frozen as
read-only mount points — it writes its session state into the profile
*directory* around them.

## Remaining limitations

- **The model itself is scripted, not real.** The turns above drive the real
  Codex CLI, the real hook engine, real operator-established trust and the real
  image-owned bridge; what they do not use is a real model, because that would
  require credentials. The provider is a loopback Node server emitting a fixed
  `exec_command` call, so what is proven is the *tool-call → hook → decision*
  path rather than any property of model behavior. That is the path the control
  boundary depends on. Codex capability nonetheless stays `hooks: partial` /
  `worktreeIsolation: partial`
  ([guardrail-parity-codex.md](guardrail-parity-codex.md)) for the other reasons
  recorded there.
- **The TUI trust step is version-shaped.** §12 drives the shipped TUI by
  waiting for its prompt text, so a Codex release that reworks the hook-trust
  prompt fails the image smoke test with "could not drive the shipped TUI to the
  hook-trust prompt" rather than silently degrading. That is the intended
  signal: Loom's own readiness contract depends on the same prompt, so a change
  there is something to learn about at image-build time.
- **Hook trust remains operator-attested per profile** (#5005). The
  registration now names a stable image-owned path, so the operator's one-time
  trust step must be taken against that path. Because `config.toml` is bound
  **by inode**, a trust step taken on the host while a session is running is not
  visible inside that session, and the next admission refuses on the profile
  digest mismatch: **stop and start the session after establishing trust.** That
  is the fail-closed direction, and it is the same restart the registration's
  own lifecycle already needs.
- **The protection is per-file, not per-directory.** A control file that Loom
  does not know about — a future Codex file carrying policy-relevant state —
  would be writable until it is added to `PROFILE_CONTROLS` (which is a
  `CONTROL_VERSION` bump). `private_workspace::control`'s audit of
  MCP/plugin/alternate-profile routes still runs before every launch, over every
  documented config layer, so a new *route* is refused even if a new *file* is
  not yet frozen.
- **A session created before control v2 is refused, not upgraded.** There is no
  way to add a mount to a running container, so an existing private session must
  be stopped and started again to gain the protection. Both halves refuse it in
  the meantime (`profile-mutable`, and the host's mount-inventory check).
- **No fleet enablement.** This is a prerequisite for #8787; #4496 remains the
  live production gate, and the shipped Codex manifest still refuses mutable
  lifecycle roles.

## Authentication is untouched

`auth.json` stays writable in the canonical profile bind mount (0700 profile /
0600 auth, uid 1000), no credential byte enters the bundle, the manifest, the
clone, exported metadata, host logs or an image layer, and no profile
permission is loosened. The control-file binds are deliberately **per file**
rather than a read-only profile mount for exactly this reason: `auth.json` and
the directory around it are untouched, so the canonical refresh — write a
sibling temp, `rename` over — still works byte for byte. The integration test
performs that refresh from inside the session in the same breath as it proves
the three control files are frozen, and asserts the host-side file changed
atomically while none of the three moved. The host-side provisioning step never
reads, writes or copies `auth.json`, and runs with `--network none`.

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
   `control` field, which the older daemon ignores. A session *created* by the
   newer daemon carries three extra read-only mounts; an older daemon's
   `validate_settings` counts mounts exactly and will refuse to reuse it, so
   **stop the session before rolling the daemon back** — it is refused, never
   silently reused in a shape the old daemon does not understand.
3. **Never** work around a refusal by deleting the account volume, resetting a
   branch, loosening profile permissions, chmod-ing the bundle writable,
   dropping the control-file binds, or passing
   `--dangerously-bypass-hook-trust`. Every refusal preserves the session, the
   volume and the profile for inspection.

Host sessions, shared-mount sessions, the global `hooks` configuration and
`worktreeIsolation: partial` are all unchanged by this boundary: it applies only
to a private-clone session whose image ships the bundle.
