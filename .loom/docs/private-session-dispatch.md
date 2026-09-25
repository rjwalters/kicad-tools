# Dispatch into account-private Codex clones

Private sessions are opt-in through `loom-daemon accounts session start NAME
--private-clone HTTPS_URL`. Scheduled roles and explicit/scheduled sweeps select
an account once, prepare its private clone, and carry that selection and the
exclusive account job lease into the normal `spawn-worker` → `spawn-codex` →
supervised `session-exec` chain. Preparation runs outside scheduler/registry
locks and before issue claims. A busy account or unavailable session is reported
without launching a model or keeping a temporary issue reservation.

This transport does not promote Codex capabilities. The shipped manifest still
rejects mutable lifecycle roles pending #8787 and the live canary in #4496.
Read-only roles admitted by the current manifest can use private sessions.
Claude and legacy host-mounted Codex sessions retain their existing transport.
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
That transport fixture is not evidence of production mutable-role admission.
The existing `session_exec_docker` suite remains the process-lifetime regression
gate for cancellation, killed/stalled owners and missing/hung cleanup.
