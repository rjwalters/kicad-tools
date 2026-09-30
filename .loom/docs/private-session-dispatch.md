# Dispatch into account-private Codex clones

Private sessions are opt-in through `loom-daemon accounts session start NAME
--private-clone HTTPS_URL`. Scheduled roles and explicit/scheduled sweeps select
an account once, prepare its private clone, and carry that selection and the
exclusive account job lease into the normal `spawn-worker` → `spawn-codex` →
supervised `session-exec` chain. Preparation runs outside scheduler/registry
locks and before issue claims. A busy account or unavailable session is reported
without launching a model or keeping a temporary issue reservation.

This transport does not promote Codex capabilities, and the shipped manifest
still declares `worktreeIsolation: "partial"` / `hooks: "partial"`. Since #8787,
a **verified** private clone can nonetheless satisfy the one requirement
`worktreeIsolation` for a single Builder / Doctor / sweep-lifecycle launch, on
measured evidence rather than on configuration — see
[guardrail-parity-codex.md](guardrail-parity-codex.md) § "Verified private-clone
containment" for the proof, the obligation-to-mechanism table, the supported
image/CLI/protocol combinations, the remaining limitations and rollback. #4496
remains the live production go/no-go and no fleet default changes.
Read-only roles admitted by the current manifest can use private sessions
exactly as before. Claude and legacy host-mounted Codex sessions retain their
existing transport **and their existing admission**: containment is asked about
only when a rejection's sole unmet requirement is `worktreeIsolation` on Codex,
and it is proven per launch, never cached.
Private selections reject `LOOM_CODEX_SESSION_EXEC=0` and
`LOOM_SPAWN_NO_EXPORT` before preparation or claim. Direct adapter entry applies
the same guard before any Codex probe, including inherited leases and symlinked
profile paths. An owned profile missing its session marker also stops for
recovery. Nonprivate escape flags and `LOOM_CODEX_NO_EXEC` previews are unchanged.

## Private context and control boundary

The worker's cwd and project root are `/workspace/repo`; worktrees and installed
helpers resolve inside that clone. **Guard code and effective guard policy do
not**: they come from the image-owned, digest-sealed
`loom-private-control-v1` bundle at `/opt/loom/private-control/`, whose identity
is bound to the account/container/lease at admission and rechecked immediately
before spawn and again in-container before the model is exec'd. The account
profile's hook registration, Codex trust state and readiness receipt are bound
**read-only over their own paths**, so they cannot be written, removed or
renamed from inside the session at all, while the profile directory stays
writable for the canonical `auth.json` refresh — see
[private-control-bundle.md](private-control-bundle.md) for the supported
image/CLI/protocol combinations, the forced policy map, the residual limitations
and rollback. The host retains the logical repository identity for dispatch,
status and log collection. Account credentials stay in the external account
profile; forge credentials are forwarded only to private Git/forge processes and
never written into exports.

Private v1 refuses the SSH/host/Docker `run-job` executor. Run supported builds
directly inside the clone. No host repository, Docker socket or daemon-control
socket is mounted. Host executor and control-routing environment variables are
not forwarded. Host worktree reapers skip issues with private ownership records;
a same-named host worktree is never evidence of private job ownership.

The worker audits account, project/ancestor and system Codex configuration before
launch. It refuses configured MCP servers, plugins/marketplaces, alternate
profiles and agent config files, preserving the operator's configuration. CLI
profile/path/remote-executor selectors and configuration overrides other than
model/effort are also refused. This deliberately narrow v1 surface prevents a
cloned project from reintroducing a host-control MCP endpoint. The audited config
layers follow the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-basic)
and [advanced configuration](https://learn.chatgpt.com/docs/config-file/config-advanced).

## Mutable-role admission (issue #8787)

Admission happens **before** account selection on every path, so a launch that
containment could satisfy is admitted in two steps, inside one decision:

1. Static admission runs unchanged. Only a rejection whose runtime is Codex and
   whose **sole** unmet capability is `worktreeIsolation` is eligible to ask.
2. A `containment::Preparer` then prepares exactly one private selection —
   the same `dispatch::Selection` the launch will use, holding the same
   exclusive account lease; there is no second account-selection pass — and
   re-derives the proof from it. A candidate the ordered runtime-preference walk
   subsequently passes over releases that selection (and its lease) before the
   walk continues, and a contained admission that reaches the launch site
   **without** its selection is refused rather than re-prepared.

An explicit operator pin still disables fall-through: containment may satisfy
the pinned runtime's own requirement, but a refusal never reroutes the launch to
a different runtime. The same two-step shape is applied by
`sweep_registry::private_dispatch` (unlocked sweep preparation), the role
runner's tick, and `worker_spawn`'s direct adapter entry, and is repeated
independently by `private-workspace execute` inside the container before the
model is exec'd.

`spawn-codex.sh`'s mutable-role hook preflight is **relocated, not skipped**,
for a private-clone launch: the managed registration it would check lives in the
account profile and names an image-owned bridge path that does not exist on the
host, so verifying it there would evaluate the wrong bridge. The audit line says
`hooks=verified-in-private-session`, and the identical obligation — registration
present, naming the sealed bridge, receipt pinning it, profile trusted — is
proven in-container instead. `--dangerously-bypass-hook-trust` is passed nowhere.

`loom-daemon accounts session status NAME --json` reports the containment
provenance for the current job under `admission` (mode, satisfied requirements,
the manifest's own native values, account name, short container and control
identities, protocol, base revision). It is retired with the lease it belongs to
and carries no credential, no profile path and no raw operator configuration.

## Durable state and recovery

Before launch, the host writes `.loom/private-jobs/issue-N.json` (or a hashed role
job key), associating runtime, provider, account, logical repository, job owner,
container ID and private volume. Host log paths remain usable after cancellation
or container loss. The job's exclusive file descriptor survives dispatch into
the supervisor; a durable account job record remains if process/cleanup state is
uncertain. Redispatch must prove the original container has no remaining writer.

A bounded read-only snapshot exports only expected issue/branch identity,
revision/publication status, dirty status and checkpoint fields. The host chooses
the checkpoint destination from trusted dispatch identity. Unexpected fields,
oversized metadata, foreign issue identities and symlink destinations are refused.
Unchanged checkpoints retain their original timestamp and do not fabricate
progress for crash-budget accounting.

Dirty or unpushed private work remains in the account volume. Restart can resume
on that account after the lifetime/cleanliness checks; automatic account or
runtime failover stops for explicit recovery of the account-owned checkpoint.
A successful remote push alone does not transfer private local phase state.
Never delete the volume or reset a branch to work around a recovery refusal.

## Credential-free integration evidence

`private_workspace_docker` uses real Docker, Git over authenticated local HTTPS,
production adapters/helpers and the complete Linux daemon. Its synthetic model
and forge CLIs do not contact a model service or production account. It verifies
admitted scheduled guide dispatch, mutable sweep refusal before claim, and an
issue-scoped synthetic worker's branch/push/PR/checkpoint/log/cancellation path.
Its `containment` module additionally drives #8787's admission through the
production preference resolver and the real adapter chain: bare-host,
unmanaged-clone, untrusted-profile and replaced-container sessions are each
refused with their own obligation, and a verified session runs a mutable role
whose issue-branch commit and push land while the protected remote ref, the host
checkout, the host sibling directory and the peer account's profile and volume
are all unchanged. The synthetic hook-trust decision there stands in for the
operator's one-time TUI step; the real TUI, the real CLI and the real hook
engine are measured by `docker/session/test-image.sh` §12.
That transport fixture is not evidence of production mutable-role admission.
The existing `session_exec_docker` suite remains the process-lifetime regression
gate for cancellation, killed/stalled owners and missing/hung cleanup.
