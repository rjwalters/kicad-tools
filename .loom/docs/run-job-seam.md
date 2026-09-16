# The `run-job` seam — docker-backed work without a docker socket

**Status**: shipped (epic #6896 Phase 4, issue #7853)
**Implementation**: [`run-job.sh`](../scripts/run-job.sh) (client) +
[`lib/run-job-exec.sh`](../scripts/lib/run-job-exec.sh) (executor)
**Tests**: `defaults/scripts/tests/test-run-job.sh` (CI-wired, hermetic — fake
docker + fake ssh, no daemon and no network required) and
`docker/worker/test-run-job.sh` (CI-wired, real — see
[Proof](#how-this-is-proven) below)

## Why this exists

A Loom worker container gets **no docker socket** — not mounted, not
reachable. That is the whole security rationale of the containment boundary
epic #6896 Phase 3 shipped: a mounted `/var/run/docker.sock` is
**host-root-equivalent**, so handing one to an LLM-directed agent inside a
containment boundary defeats the boundary
([ADR-0017](https://github.com/rjwalters/loom/blob/main/docs/adr/0017-session-container-architecture.md) Decision 3;
operator decision, 2026-08-24).

But agents inside worker containers legitimately need docker-backed work done:
build-gate toolchains, Lean builds, SPICE simulations. The `run-job` seam is
the sanctioned way to get it: the agent sends a **job spec** to an **executor**
that already runs on a real docker host, and the job's logs and exit code come
back faithfully.

```
  worker container                  executor host (a real docker host)
  ┌──────────────────────┐          ┌───────────────────────────────────┐
  │ agent                │          │                                   │
  │  └ run-job.sh ───────┼── ssh ──▶│ bash -s  (run-job-exec.sh, piped) │
  │      (no docker,     │  or      │   └ docker run --detach …         │
  │       no socket)     │  local   │        └ job container            │
  └──────────────────────┘          └───────────────────────────────────┘
        ▲ logs (stdout/stderr) + exit code, passed through verbatim
```

**Out of scope, by design**: *where* executors live and how executor capacity
scales. That is elastic compute, #3979 — this seam is the concrete consumer
that proposal was waiting for, and it hands off there. This document settles
that the seam exists, what a job is, and that it is socket-free.

## The job spec (`loom.run-job/v1`)

A job is one JSON object. Every field below is part of the contract; anything
not listed is not accepted, which is how the seam stays auditable.

```json
{
  "schema": "loom.run-job/v1",
  "id": "job-20260916T120000Z-a1b2c3",
  "image": "ghcr.io/rjwalters/loom-worker:latest",
  "command": ["bash", "-lc", "cargo build --release"],
  "workdir": "/home/loom/workspaces/loom",
  "network": "none",
  "mounts": [
    { "path": "/home/loom/workspaces/loom", "mode": "rw" },
    { "path": "/home/loom/.loom/tokens", "mode": "ro" }
  ],
  "env": { "CARGO_TARGET_DIR": "/home/loom/workspaces/loom/target" },
  "limits": { "cpus": "4", "memory": "8g" },
  "timeoutSeconds": 1800
}
```

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Exactly `loom.run-job/v1`. A future incompatible shape gets a new version string, never a silently-reinterpreted old one. |
| `id` | filled in | `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`. The job's handle for `attach`/`status`/`cancel`; generated when absent. The container is named `loom-job-<id>`. |
| `image` | yes | Any image reference the executor host can pull or already has. |
| `command` | yes | Non-empty argv array. Not a shell string — no quoting ambiguity across the transport. |
| `workdir` | no | Absolute path inside the job container (normally a parity-mounted path). |
| `network` | no | `none` (default) or `bridge`. **`host` is not offered.** |
| `mounts` | no | Array of `{path, mode}` — see the parity rule below. `mode` is `ro` or `rw` (default `rw`). |
| `env` | no | String map. Keys must match `^[A-Za-z_][A-Za-z0-9_]*$`. |
| `limits.cpus` | no | `docker run --cpus` value. Omitted ⇒ unbounded. |
| `limits.memory` | no | `docker run --memory` value (`8g`, `512m`, …). Omitted ⇒ unbounded. |
| `timeoutSeconds` | no | `0` (default) = no timeout. Otherwise the executor issues a **graceful** `docker stop` after N seconds. |

### Mounts are path-parity by construction

A mount is **one absolute path plus a mode** — never a `src:dst` pair. The
executor always binds it at the *identical* absolute path inside the job
container:

```
{"path": "/home/loom/workspaces/loom", "mode": "rw"}   ->   -v /home/loom/workspaces/loom:/home/loom/workspaces/loom
{"path": "/home/loom/.loom/tokens",    "mode": "ro"}   ->   -v /home/loom/.loom/tokens:/home/loom/.loom/tokens:ro
```

This is [`docker/worker/MOUNT-CONTRACT.md`](https://github.com/rjwalters/loom/blob/main/docker/worker/MOUNT-CONTRACT.md)
§1 (path parity) expressed as a *data shape* rather than a rule callers are
asked to remember: there is no non-parity mount you can express in a job spec.
Git worktrees store absolute paths in their `.git` pointer files, so a job that
builds or commits inside `.loom/worktrees/issue-N` works unchanged — and a
`CARGO_TARGET_DIR` (MOUNT-CONTRACT.md §4) resolves identically on the host and
inside the job.

The corollary the caller still owns: **the executor host must actually see
those paths at those paths.** On a loopback/same-host executor that is free.
On a remote executor it means the workspace is on shared storage, or the job
is one that needs no workspace (an image build, a simulation over mounted
inputs). This seam does not replicate filesystems.

### What a spec can never express

`--privileged`, `--cap-add`, `--device`, `--security-opt`, `--pid=host`,
`--user 0`, host networking, and — the load-bearing one — **any bind of a
container-runtime socket** (`docker.sock`, `containerd.sock`, `podman.sock`,
`crio.sock`, anything under `/var/run/docker*` or `/run/docker*`). `/proc`,
`/sys` and `/dev` are refused too. There is no field that maps to them, and
the validator rejects the socket paths explicitly with the rationale in the
message.

**The refusal matches the *resolved* path, not just the spelling.** Docker
resolves a bind mount's source on the executor host at run time, so a check
against the literal string alone would not be a boundary at all: a symlink
whose own name matches nothing —
`/srv/work/innocent.sock -> /var/run/docker.sock` — would put the real socket
inside the job container. An agent inside a worker container can create
exactly that link in any `rw` parity mount it already holds. So the validator
canonicalizes each mount source and **refuses any mount whose real path
differs from the given path**, then matches both spellings against the
refusal patterns above. A symlink is refused rather than silently rewritten
because the seam's mount shape is one path bound at the identical path
(MOUNT-CONTRACT.md §1) — substituting the target would quietly break the
parity guarantee. Pass the resolved path explicitly instead; the error message
names it.

**The refusal also covers the socket's *ancestors*.** A bind mount propagates
a directory's entire contents, socket special files included, so `--mount
/run` — the real, non-symlink home of `docker.sock` on every mainstream
distro, and a name that matches none of the socket patterns — would carry
the socket into the job container just as surely as naming it. The validator
therefore refuses any mount whose source (given or resolved) is a proper
ancestor of a known container-runtime socket path: `/run`, `/var`,
`/var/run`, `/run/podman`, `/run/containerd`, and so on. This is a string
check against a fixed list, so it holds on the client's pre-flight as well as
on the executor, whether or not the socket exists yet.

**Which is why a mount path must be spelled canonically.** The ancestor check
is a prefix match, and a prefix match is spelling-sensitive: `/run//`, `//run`,
`/run/.` and `/run/./` all name `/run`, yet none of them is a prefix of
`/run/docker.sock`. The executor collapses such a spelling before matching
(its `readlink -f` resolves the path on the host that will do the mounting),
but the client cannot resolve a path that does not exist on the *client* host
— so the pre-flight would have let `--mount /run//` through on a macOS client
dispatching to a Linux executor. The validator therefore refuses a mount path
containing a `//` or `/.` segment outright, alongside the existing `..`
refusal, before any of the checks above run (#7896). One trailing slash is
still fine (`/srv/work/`). That refusal is what makes the sentence above true
on the client rather than only on the executor.

**Behind the name-based checks sits a live-socket walk.** Whatever a socket
is called and wherever a daemon was told to put it (`-H unix:///srv/x.sock`,
rootless docker under `/run/user/<uid>`), the validator refuses a mount
source that *is* a live unix socket or a directory that *contains* one on
the host doing the validation. That layer is best-effort by nature: it is
only meaningful on the executor host, it sees only sockets that exist at
validation time, and it skips subtrees it cannot read.

Validation runs **twice, from one implementation**: `run-job.sh` sources
`loom_job_validate_spec` from the executor program for its client-side
pre-flight, and the executor runs the same function again on its own host. A
client that skips its pre-flight, or lies, still cannot talk an executor into
a privileged container *through any input the spec can express*. The
executor-side run is also the only one whose path resolution and socket walk
are meaningful — it is the host that will do the mounting — which is why the
re-validation is load-bearing rather than merely belt-and-braces.

What the seam does **not** claim: the checks are evaluated at validation
time on the executor's filesystem. A socket created *after* validation
inside an `rw` mount the job already holds, or a socket the executor host
cannot see (unreadable subtree, non-standard location that is not live yet),
is outside what a pre-run validator can refuse; the containment there rests
on the job container being an ordinary unprivileged docker container (no
`--privileged`, no added capabilities, no host namespaces — none of which the
spec can express), not on the mount check alone.

## Using it

```bash
# Build a spec from flags (the common case)
.loom/scripts/run-job.sh \
  --image ghcr.io/rjwalters/loom-worker:latest \
  --mount "$PWD" --workdir "$PWD" \
  --cpus 4 --memory 8g --timeout 1800 \
  -- bash -lc 'cargo build --release'

# Or hand it a spec file (or `-` for stdin)
.loom/scripts/run-job.sh --spec job.json

# Inspect without running
.loom/scripts/run-job.sh --print-spec --image alpine --mount /srv -- true
.loom/scripts/run-job.sh --dry-run   --image alpine --mount /srv -- true

# Lifecycle
.loom/scripts/run-job.sh status <job-id>
.loom/scripts/run-job.sh attach <job-id>    # re-stream + wait for the exit code
.loom/scripts/run-job.sh cancel <job-id>    # graceful stop
```

`--dry-run` prints the exact `docker run` argv the executor will run, one
argument per line — an operator can audit a job without running it, and the
argv is produced by the *same* builder the executor uses, not a re-description
of it.

## Executor selection

| Precedence | Source |
|---|---|
| 1 (highest) | `--executor <auto\|local\|ssh>` |
| 2 | `LOOM_JOB_EXECUTOR` env var |
| 3 | `.loom/config.json` → `jobs.executor.mode` |
| 4 (default) | `auto` |

`auto` picks `local` when a docker daemon is reachable **from here**, and
`ssh` otherwise. Inside a worker container there is no docker and no socket,
so `auto` correctly lands on `ssh` — which is the loopback case by default:
the worker's own host.

```json
{
  "jobs": {
    "executor": {
      "mode": "ssh",
      "host": "buildbox.internal",
      "user": "loom",
      "sshOptions": ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    }
  }
}
```

| Axis | Precedence |
|---|---|
| host | `LOOM_JOB_EXECUTOR_HOST` → `jobs.executor.host` → loopback derivation (`host.docker.internal` when it resolves, else the container's default gateway, else `localhost`) |
| user | `LOOM_JOB_EXECUTOR_USER` → `jobs.executor.user` → ssh's own default |
| ssh options | `LOOM_JOB_SSH_OPTIONS` → `jobs.executor.sshOptions` → `-o BatchMode=yes -o ConnectTimeout=10` |
| ssh binary | `LOOM_JOB_SSH_CMD` → `ssh` |
| docker binary (executor-side) | `LOOM_RUN_JOB_DOCKER` → `docker` (set it to `podman` or an absolute path if needed) |

**The loopback/ssh executor is the mandatory baseline**, not an optional
deployment: a single-host install must be self-sufficient with no external
executor dependency (ADR-0017 Decision 3, "Negative" consequence — a down
remote executor blocks docker-requiring jobs, which is acceptable *only*
because the same-host executor always exists).

### Executor host requirements

`bash`, `docker` (or whatever `LOOM_RUN_JOB_DOCKER` names), `jq`, and the
coreutils that ship as one package (`base64`, `mktemp`, `mkfifo`, `readlink`).
**No Loom installation.** The ssh transport pipes the executor program itself
to a remote `bash -s`, so the executor host never has to be provisioned with,
or kept in sync with, a Loom checkout — the program that runs there is always
the one that shipped with the calling client.

## Exit codes and log passthrough

Stdout and stderr stream through live, unmodified, in their original streams —
the job's stdout is the caller's stdout, the job's stderr is the caller's
stderr.

| Code | Meaning |
|---|---|
| `0`–`255` | The job's own exit code, verbatim |
| `69` (`EX_UNAVAILABLE`) | The executor was unreachable — **the job never ran** |
| `75` (`EX_TEMPFAIL`) | The job is still running; this client detached (reattach with `attach <id>`) |
| `78` (`EX_CONFIG`) | Invalid job spec, or a misconfigured executor |

### Why a job that exits 255 is not confused with a broken ssh

`ssh` exits `255` for its own failures, and it exits with the remote command's
code otherwise — the two are indistinguishable from the exit status alone. So
the exit status is **not** what this seam trusts. The executor emits

```
# LOOM_RUN_JOB_EXIT id=<job-id> code=<n>
```

on stderr after the job's output has drained, and *that* line is authoritative.
No sentinel and no word from the executor ⇒ transport failure ⇒ `69`, never a
fabricated job result. A job that genuinely exits 255 reports 255.

The other machine-readable markers, all on stderr:
`# LOOM_RUN_JOB_START`, `# LOOM_RUN_JOB_DETACHED`,
`# LOOM_RUN_JOB_CANCELLED`, `# LOOM_RUN_JOB_TIMEOUT`.

`# LOOM_RUN_JOB_STATUS` is the exception, and deliberately so: it is not a
side-channel diagnostic accompanying a job's own streams — it *is* the entire
output of the `status` verb, so it goes to **stdout** where a caller can read
it with an ordinary command substitution.

## Restart safety (#5119 drain semantics)

Teardown is **not** cancellation. Epic #6896's Risks section requires that
executor-side teardown never SIGKILL an in-flight job on a daemon restart, and
the seam gets that from its process shape rather than from careful signal
handling:

- The job container is started **detached** and deliberately **without
  `--rm`**. It is owned by the executor host's docker daemon, not by the
  client's process tree or the daemon's cgroup. A SIGKILL'd daemon, a dropped
  ssh connection, or a hard-killed client therefore cannot stop a running job
  — the same "sweeps survive by design" property containerized dispatch
  already relies on (`runtime-adapters.md` → containerized dispatch mode).
- On a *catchable* signal (a `loom-daemon restart --drain` SIGTERM, an
  operator Ctrl-C, SIGHUP when ssh drops) both halves **detach**: they kill
  only their own observer processes (`docker logs -f`, `docker wait`), print
  `# LOOM_RUN_JOB_DETACHED`, and exit `75`. Neither half ever runs
  `docker kill`, `docker stop`, or `docker rm` on teardown.
- Because the container is not `--rm`, its exit code and full log survive the
  client's death. `run-job.sh attach <id>` re-streams the job **from its first
  line** and returns its real exit code; `status <id>` answers
  `running|exited|absent` without consuming it. The container is removed only
  once its exit code has actually been read.
- `cancel <id>` — the explicit act — is `docker stop --time <grace>`
  (SIGTERM, then docker's own escalation after the grace period,
  `LOOM_RUN_JOB_STOP_GRACE`, default 30s). This seam never invokes
  `docker kill`.

The executor awaits `docker wait` in the **background**, via the `wait`
builtin, for a non-obvious reason worth preserving: bash defers a trapped
signal until the current *foreground* command finishes, so a foreground
`docker wait` would make a drain SIGTERM sit unhandled for the entire life of
the job.

## How this is proven

Two suites, because the two claims have different shapes.

| Suite | Runs | Proves |
|---|---|---|
| `defaults/scripts/tests/test-run-job.sh` | every PR (`shell-suite-tests`) | The seam's *logic*: spec normalization and validation, the socket refusal on both sides, the generated `docker run` argv, exit-code and sentinel handling, executor selection, restart-safe detach/reattach. Hermetic — a fake docker and a fake ssh, no daemon, no network. |
| `docker/worker/test-run-job.sh` | every docker-touching PR (`worker-image-smoke`), and on every push | The *claim*: a `run-job` client running inside the shipped `loom-worker` image — with no docker socket, asserted from inside the container — starts real job containers on the host's real docker daemon through a loopback executor, and gets the real logs and the real exit code back. |

The second suite fakes exactly one thing, the ssh hop: a CI runner has no sshd
a container can log into, so `LOOM_JOB_SSH_CMD` points at a file-dropbox
transport that relays what ssh relays (the executor program on stdin, the verb
and args in argv, stdout/stderr/exit status back) and emulates a dropped
connection's SIGHUP when the client dies. Everything above the transport — the
client, the executor, docker, the job container, the parity mounts — is real.
It skips cleanly (exit 0) where docker is unavailable, like its `test-image.sh`
and `test-mount-contract.sh` siblings.

The restart-safety case is the one worth reading: it SIGKILLs the *client
container* mid-job (the daemon-restart analogue), then shows the job container
still running on the host, and `attach` recovering both its full log and its
real exit code afterwards.

## Caller migration (#7854)

Issue #7854 — the sibling Phase 4 issue this one names above — audited every
shipped script and doc for a docker-requiring *caller* in the worker-container
path (a build-gate toolchain invocation, a Lean/SPICE-style sim/build wrapper)
that shells out to `docker` directly and needs moving onto this seam. It found
**none in this repository**: this repo's own `buildGate.command`
(`defaults/scripts/build-gate.sh`, see [`build-gate.md`](build-gate.md)) is
`cargo test` + a bash test suite and needs no docker at all, and Loom's own
worker image deliberately ships no language toolchain for a downstream build
step to wrap (`docker/worker/README.md` § "What this image deliberately does
NOT include" — "per-repo build-gate toolchains are a downstream `FROM` layer's
job"). Lean and SPICE, named in the epic as the motivating examples, are
themselves downstream-repo concerns, not code that lives here.

So there was nothing left in-tree to migrate — the seam's own socket refusal
(above) already makes the anti-pattern this migration exists to close
structurally unavailable, not just discouraged. What #7854 *does* add, given
that:

- **The sanctioned pattern, documented and worked.** [`build-gate.md`](build-gate.md)
  § "Docker-requiring toolchains (Lean, SPICE, or similar)" is the copy-paste
  starting point for a downstream repo (or a future in-repo need) whose
  `buildGate.command` — or any other worker-container-path step — needs a
  docker-backed toolchain: wrap it in `run-job.sh` rather than assuming a
  local docker socket.
- **A standing regression guard**, not just a point-in-time audit:
  `defaults/scripts/tests/test-run-job.sh` § 18 greps every shipped script for
  a literal `docker run`/`docker exec` outside the two files that are
  legitimately exempt (`spawn-claude.sh`'s containerized dispatch and
  `spawn-codex.sh`'s session-exec mode — both HOST-side, launching a
  container from the daemon's own host rather than running inside one). A
  future build-gate stage or sim/build wrapper that reaches for `docker`
  directly instead of this seam fails CI at the point it is added, rather than
  drifting unnoticed the way the pre-#7853 state did.

## What is NOT here yet

- **Elastic executor placement** — #3979, explicitly out of scope.
- **Daemon-side job registry.** Jobs are addressable by id through
  `status`/`attach`, but `loom-daemon status` does not yet enumerate in-flight
  jobs the way it enumerates sweeps, and nothing reaps an abandoned job
  container whose exit code was never read.

## Related

- [ADR-0017](https://github.com/rjwalters/loom/blob/main/docs/adr/0017-session-container-architecture.md) — Decision 3
  is the "never docker-in-docker, never socket passthrough" ruling this seam
  implements; Decision 4 is the restart-safety contract.
- [`docker/worker/MOUNT-CONTRACT.md`](https://github.com/rjwalters/loom/blob/main/docker/worker/MOUNT-CONTRACT.md) —
  the path-parity, secrets and build-cache rules job mounts follow.
- [`runtime-adapters.md`](runtime-adapters.md) — worker dispatch, containerized
  dispatch mode, and the per-sweep resource limits this seam's job limits
  mirror.
- Epic **#6896** — session containers; this is its Phase 4.
- **#3979** — elastic compute (executor placement); the handoff line.
