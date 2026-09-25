# Guardrail parity: Pi, OpenCode and Kimi

Loom-managed native roles use four tools: `loom_read`, `loom_write`,
`loom_edit`, and `loom_bash`. Their small harness bindings call the Rust
`loom-daemon runtime-tool` service. Policy is not copied into either binding:
Rust normalizes requests through the existing shared guard bridge and its
workflow, destructive-command, and worktree policies.

Tested versions and live outcomes: [verification receipt](native-runtime-verification-2026-09-19.md)
— Pi 0.85.1 and OpenCode **1.18.31**.

| Harness | Pinned version | What that pin rests on |
| --- | --- | --- |
| Pi | 0.85.1 | Guarded live canary — [verification receipt](native-runtime-verification-2026-09-19.md). |
| OpenCode | 1.18.31 | Guarded live canary (1.x only) — same receipt; see "OpenCode major versions". |
| Kimi | 2.0.2 | **No guarded canary yet.** Credential-free harness probe only — [`docs/experiments/kimi-harness-probe-2026-09-22.json`](https://github.com/rjwalters/loom/blob/main/docs/experiments/kimi-harness-probe-2026-09-22.json) (#8561). The pin names the CLI the adapter and the container image were built against, *not* a verified guard boundary; see "Kimi". |

`docker/native/Dockerfile` equality-checks all three at build time (`ARG
OPENCODE_VERSION` / `PI_VERSION` / `KIMI_CODE_VERSION`), so a drifted pin fails
the image build rather than shipping silently. Bump a pin, this table and a
fresh run together; never one without the others.

Everything this page says about OpenCode's
guard was verified on OpenCode 1.x only. **No OpenCode 2.x guarded receipt
exists yet**, so a guarded launch on 2.x is refused before spawn; see
"OpenCode major versions" below. Kimi's binding (#8562) is implemented and
unit-tested but has **no live guarded canary receipt at all yet** — every
capability in `defaults/runtimes/kimi.json` stays `"no"` and Builder/Doctor/
Judge stay refused on Kimi until one lands; see "Kimi" below.

## Enforcement boundary

| Intent | Implementation |
| --- | --- |
| Native edits stay in managed worktrees | Every write/edit target is checked by the existing worktree policy before access. Shell commands use the existing shell-write policy. |
| Protected branches and workflow rules | The same destructive and workflow guards used by the existing runtimes inspect each shell call. |
| Broken guard cannot permit a tool | Missing guard files, malformed/unknown output, and nonzero exit refuse the operation with a `policy error:`-prefixed message. A policy timeout (default 20s, see below) refuses too, but is reported with a distinct `policy timeout:` prefix — see "Policy timeout vs. denial" below. |
| Binding fails to load | Pi starts with builtin tools and extension discovery disabled. OpenCode uses a dedicated primary agent whose default permission is deny, with only the four named tools enabled. Missing bindings therefore leave no executable unguarded tool surface. *(OpenCode: verified on 1.18.31 only.)* A role tick that exits 0 having used no `loom_*` tool is additionally reported as a failed tick, not a success — see "Toolless launch detection". |
| Concurrent file edits | File operations share a workspace mutation lock; an edit must match exactly one nonempty old-text occurrence. |
| Large output and hung commands | Reads/output are bounded; shell execution uses the existing Rust bounded process executor and a maximum 600-second deadline. SIGTERM/SIGINT cancels the owned shell process group. |
| Model/provider selection | Existing named profiles and explicit provider/model selections; no Claude token-pool preflight or implicit Sonnet default on native sweeps. |
| Credentials | Profile environment/pool mapping, or an explicitly selected external auth snapshot; keys are never embedded in binding source or arguments. |

Bindings and mutable harness state are generated outside repositories under
`~/.local/state/loom/native-tools/<workspace-hash>/<launch-id>/`. Every launch
has its own private directory (0700), including concurrent launches in one
workspace. `LOOM_NATIVE_TOOLS_DIR` selects an alternative **external base**,
including the existing container-private base; it no longer selects a shared
binding directory. Pi's agent/auth and session directories, and OpenCode's
data/state/cache directories are pinned beneath the launch directory.

**One exception, and only for immutable content (#8663):** OpenCode's
`OPENCODE_CONFIG_DIR` is a content-keyed tree at
`<workspace-hash>/bindings/<digest>/opencode/`, shared by every launch of that
workspace whose bindings hash identically (the plugin source is `include_str!`'d
and its dependency is pinned, so the bytes are fixed by the daemon build). It
holds the generated `plugins/loom.ts`, the pinned `package.json`, and the
`node_modules/` the CLI resolves from them — ~126 MB and ~7,300 files that were
previously re-installed per launch and never removed (30–37 GB per fleet host,
two ENOSPC incidents). Nothing mutable is shared: auth snapshots, sessions,
transcripts and every XDG directory stay inside the launch's own 0700
directory, so two concurrent workers still share no credential or session
state. The tree is published by an atomic rename of a fully written staging
copy, and its two binding files are re-written (idempotently) on every launch,
so a damaged tree self-heals instead of serving content no launch provisioned.

Stale launch directories are reaped by the next launch for the same workspace
and by `loom-daemon clean` (see "Reaping stale native launch state" below).
Absolute paths are required. Paths inside this workspace or another Git
checkout, including symlink aliases, are refused before binding provisioning;
unsafe inherited HOME, Pi, OpenCode and XDG directory overrides also refuse
launch. The workspace mutation lock remains operational repository state.

Profile environment and API-key pool injection keep their existing precedence.
Guarded launches no longer implicitly reuse a harness's global auth store.
For an uncontained OAuth or existing-login launch, set `LOOM_NATIVE_AUTH_FILE` to that
harness's external JSON auth file (owned by you, mode 0600, in a 0700 directory,
at most 1 MiB). Its credential type and required fields must match the selected
harness; wrong formats fail with a fixed diagnostic that omits credential values.
Loom copies it into the private launch directory; the original is never changed,
moved or deleted. Container launches retain environment/pool injection; this
host snapshot option does not add a secret mount. Refreshes affect only the
launch copy. This is a snapshot, not persistent login synchronization: later
launches may need renewed authentication, and concurrent refresh behavior depends
on the provider. Prefer profile environment/API-key pool injection for repeated
workers. Renew the external source separately and use the matching harness's
auth format. With no
injected credentials or snapshot, Loom emits migration guidance; unauthenticated
local providers still work and remote providers report their own missing-auth
error. Provider/model selection and fallback policy are unchanged.

The private launch state is retained for inspection and may contain credentials,
sessions and logs: treat it as secret, keep its directories private, and remove
completed launch directories when no longer needed. There is no automatic
migration or deletion of existing repository-local state. Direct unguarded
launches and interactive harness sessions keep their previous behavior.

The OpenCode binding depends on the matching
`@opencode-ai/plugin` package; OpenCode installs it into that isolated config
directory. That package is pinned to 1.18.31 and was verified against an
OpenCode 1.18.31 host only; whether a 2.x host loads it is unverified. No
global CLI model/login settings are rewritten by Loom dispatch.

## Policy timeout vs. denial (#8451)

The guard bridge runs as a subprocess (`guard-codex-bridge.sh`, which itself
forks `guard-loom-workflow.sh` and `guard-destructive.sh`) under a bounded
deadline in `loom-daemon/src/native_tools/guard.rs`. On a CPU-saturated host
that fork chain can outrun the deadline before it ever produces a decision —
observed live on a host at load average 55/28 cores (another tenant's test run
holding ~19 cores, not an exec-scan or a guard defect): 4 of 21 `loom_bash`
calls refused in the first 9 minutes of a sweep, including plain `cat
.loom/config.json`.

Both outcomes fail closed (no tool runs either way), but they mean different
things to the model driving a native sweep, so every `loom_bash`/`loom_read`/
`loom_write`/`loom_edit` failure text carries a class prefix:

| Prefix | Meaning | Retry? |
| --- | --- | --- |
| `policy denied: <reason>` | The check ran to completion and the shared guards said no. | No — this is a real refusal; change the command, not the wording. |
| `policy timeout: …` | The check did not finish inside the budget; the command was never evaluated. | Yes — transient, most likely host load. Retrying the identical command is reasonable once. |
| `policy error: …` | The guard scripts are missing, crashed, or returned something the bridge could not parse. | Treat as a refusal (fail closed), but it is a provisioning defect, not a policy decision about this command — do not keep retrying the same command in a loop. |

[`native-sweep.md`](native-sweep.md) tells the model to apply this distinction
directly.

### Configuring the budget

The fixed 20-second budget was chosen on an idle host and is now configurable,
still bounded and fail-closed at the limit (never "wait forever"):

- `guards.nativePolicyTimeoutSecs` in the effective (tiered) `.loom/config.json`.
- `LOOM_NATIVE_POLICY_TIMEOUT_SECS` env var, which outranks the config key
  (env > config > default, the repo's usual precedence).
- Default `20`; both sources are clamped to `[5, 120]` seconds, so a stray
  value (e.g. `0`, or a typo like `99999`) cannot turn the guard into an
  instant-refuse or an unbounded hang.

### Per-worker timeout counter

Every policy timeout appends one line to
`.loom/native-tools/policy-timeouts.jsonl` (`{"worker_pid", "budget_secs",
"at"}`), keyed by `LOOM_NATIVE_WORKER_PID` — the stable per-sweep identity
`native-sweep.md` already uses for session liveness. A starving sweep is
therefore visible by reading that file (or counting lines for its own worker
pid via `loom-daemon`'s `policy_timeout_count` helper) rather than only from
scrollback. This is a best-effort local log, not a daemon-side telemetry
record; a host too saturated to append one small file has bigger problems
than a missed counter increment.

### Read-only fast path reachability

`guards.readOnlyFastPath` (default on) is implemented inside
`guard-destructive-generic.sh` itself, and `guard-destructive.sh` (which the
bridge's `shell_command` branch runs) `exec`s straight into it — so a shell
command's destructive-guard leg **does** reach the fast path today; an
in-workspace `cat`/`ls`/`grep` skips the expensive parts of that leg (the git
`rev-parse` and the deny/ask array scan) before ever forking further. The
bridge's *other* forked guard for a shell command,
`guard-loom-workflow.sh`, always runs in full — it has no fast path of its own
and is out of scope here, since it is a separate, comparatively cheap
protected-branch/workflow check, not the destructive-command analyzer this
issue is about.

## OpenCode major versions

The adapter probes `opencode --version` before `exec` and tracks, in code, which
majors have a **live guarded-canary receipt** (`Major::guard_verified` in
`loom-daemon/src/worker_spawn/opencode_version.rs`). Today that is 1.x only. A
guarded launch — `LOOM_ROLE` set, or a `/loom:<role>` prompt — on any other
major is refused before spawn with exit 78, before any binding is provisioned.
Unguarded free-form launches on 2.x are unaffected. This is evidence tracking,
not a setting: there is no configuration key or environment override, and a
major is added only by the change that also adds its dated receipt here.

Why a passing fake-CLI test suite is not enough: OpenCode 2.x documents
`run --auto` as approving every permission that is *not explicitly denied*, and
Loom passes `--auto` for `--dangerously-skip-permissions`. If 2.x ignores any of
the deny-by-default agent, top-level `permission`, or `tools` keys Loom injects,
a role launch would run OpenCode's built-in write and shell tools, auto-approved
and outside Loom's guards — and still exit 0. Fixture tests prove Loom *sets*
that configuration, never that a real CLI *honors* it. Only a live canary that
includes the deliberately-broken-binding case (pass condition: no file written
and no executable unguarded tool, not the exit code) distinguishes "failed
closed" from "fell open". Still unverified on 2.x: the isolated
`OPENCODE_CONFIG_DIR`, `plugins/loom.ts` discovery, the plugin SDK pin above,
the agent/permission/tools config keys, and `OPENCODE_CONFIG_CONTENT` being read
by the private server `--standalone` starts.

A live guarded canary was run on **OpenCode 2.0.10** on 2026-09-20 (issue #8448)
and **failed, safely**: the binding is never loaded, so the worker started with
the deny-by-default agent and no tools at all, used zero tools, recorded zero
policy denials, changed no protected bytes, did not do the task — and exited
`0` in 9s. An identical control run on 1.18.31 passed (four `loom_*` tools
offered, seven tool uses, three recorded denials, task completed). So on 2.x:
the deny-by-default configuration **is** honored under `--auto` (it fails
closed, it does not fall open), and what is broken is binding *discovery* — no
`node_modules/` appears in the isolated config dir and `opencode plugin list`
reports none, with or without an explicit `plugin` entry. The 2.x-native
mechanism for loading a local, per-launch tool binding is still unknown, so
`Major::guard_verified`'s `V2 => false` refusal stays until a passing 2.x
receipt exists.

## Kimi

Kimi Code CLI has no extension point Loom can point at a local file the way
Pi's `--extension` or OpenCode's plugin loader do; its only route for adding
tools at all is an MCP server entry. The binding (#8562) is therefore shaped
differently from Pi/OpenCode's, though the boundary intent is the same:

- **`loom-daemon native-mcp`** (`loom-daemon/src/native_tools/mcp.rs`) is a
  stdio JSON-RPC MCP server exposing exactly `loom_read`, `loom_write`,
  `loom_edit` and `loom_bash`. Every `tools/call` is normalized into the same
  `native_tools::execute` request Pi/OpenCode use — no policy decision is made
  in the MCP server itself, so it reaches the same guard bridge, worktree
  policy, destructive-command policy, mutation lock, bounded executor and
  64 KiB truncation.
- **A relocated, per-launch `KIMI_CODE_HOME`** (`native_tools::kimi`,
  wired from `provision::configure`) holds a generated `config.toml`
  (`[tools] enabled = ["mcp__<server>__*"]`, every unguarded builtin —
  `Agent`, `AgentSwarm`, `Bash`, `Edit`, `Write` — also listed in `disabled`
  as belt-and-braces), a generated `mcp.json` naming the `native-mcp` server
  with `LOOM_WORKSPACE`/`LOOM_NATIVE_TOOL_BIN`/`LOOM_NATIVE_GUARD_DIR` in its
  env, and a generated `--agent-file` (`tools: [mcp__<server>__*]`,
  `subagents: []`) — so a binding that fails to load leaves no executable
  tool, guarded or not, rather than falling through to Kimi's own unguarded
  `Bash`/`Write`/`Edit`. The MCP server name carries a per-launch nonce
  (a hash of the launch's private state directory) rather than a fixed
  `loom` name: Kimi resolves `mcp.json` in three layers, `$KIMI_CODE_HOME`
  (Loom's) plus two the worktree can carry (`<gitWorkTreeRoot>/.mcp.json`,
  `<cwd>/.kimi-code/mcp.json`), and a later layer overwrites a same-named
  entry from an earlier one — so a fixed name would let repository content
  shadow Loom's server for a *later* launch in that worktree. A relocated
  home was chosen over a project-level `.kimi-code/` overlay because
  `[tools]` and `[[hooks]]` have no project-level file at all — an overlay
  could carry the MCP server entry but not the allowlist that makes it the
  only tool, and everything written inside the worktree is content a guarded
  model may itself edit or commit.
- **A `[[hooks]] PreToolUse` entry** (`native-mcp --pretooluse-guard`) denies
  any tool call whose name is not `mcp__<server>__loom_*`. This is
  belt-and-braces only, not part of the guarantee: Kimi's hook contract is
  documented fail-**open** — any crashed, timed-out or non-2-exit hook
  defaults to allow — so it can only ever narrow what `[tools] enabled`
  already decided, never widen it.
- **Toolless detection** (#8448) is extended to Kimi's own event shape:
  `worker_spawn::launch_outcome::classify_native_stream` now recognizes the
  OpenAI-style chat messages Kimi's `--output-format stream-json` emits
  (`{"role":"assistant","tool_calls":[{"function":{"name":…}}]}`), stripping
  the `mcp__<server>__` prefix before matching `loom_*` so a role tick that
  exits 0 having reached zero guarded tools — the deliberately-broken-binding
  case — is still reported as a `Failure`, not a false success.
- A guarded launch requires a model profile with a `credentialEnv` mapping
  (the `KIMI_MODEL_*` env family): relocating `KIMI_CODE_HOME` hides the
  operator's own `config.toml`, so the config-alias route (`-m <alias>`)
  cannot resolve under a guarded launch and the adapter refuses before spawn
  rather than launch a worker that cannot pick a model.

**No live guarded canary receipt exists yet, so nothing above is admission
evidence.** `native_tools::provision::KIMI_GUARD_VERIFIED` is `false`, and
`configure()` fails closed for `runtime == "kimi"` whenever a launch is
role-tagged, independent of the binding's own fixture tests passing — the
same "fixture tests prove Loom *writes* the configuration, never that a real
CLI *honors* it" gap the OpenCode 2.x section above documents, for exactly
the same reason: only a live run, including the deliberately-broken-binding
case (pass condition is "no file written and no unguarded tool used", not the
exit code), can distinguish "failed closed" from "fell open" on the real CLI.
`defaults/runtimes/kimi.json` stays `worktreeIsolation: "no"` /
`loomControl: "no"` — so Builder, Doctor and Judge stay refused on Kimi — until
a passing receipt lands beside a flip of `KIMI_GUARD_VERIFIED` to `true`.

### Kimi under ephemeral containment (#8565)

The opt-in per-sweep container ("Residual limits" below) covers Kimi on the
same terms as Pi/OpenCode, with one harness-specific relocation:
`KIMI_CODE_HOME` is a *single* variable carrying Kimi's whole state — config,
`mcp.json`, session store, `logs/kimi-code.log`, credential store, plugins, and
the `rg`/`fd` binaries it downloads into `$KIMI_CODE_HOME/bin/` on first use.
Unset, it is `~/.kimi-code`, so N uncontained Kimi workers on one host share
one session store, one log and one credential store.

`worker_spawn::containment` therefore points it at
`/home/loom/.loom-native/<per-launch-id>/kimi` alongside the XDG bases,
`OPENCODE_CONFIG_DIR` and `LOOM_NATIVE_TOOLS_DIR`, and the image deliberately
bakes no value for it. A *guarded* launch relocates it a second time, to
`native_tools::provision`'s own 0700 per-launch directory under the (also
per-launch) `LOOM_NATIVE_TOOLS_DIR` — so the guarded-binding directory resolves
inside the container exactly the way OpenCode's `OPENCODE_CONFIG_DIR` does. The
container-level value is what an unguarded free-form trial gets.

Credentials keep the by-name-only contract: an API-key profile's
`KIMI_MODEL_API_KEY` is forwarded as `-e KIMI_MODEL_API_KEY` with no `=value`,
so it never enters the dispatch's argv or any file in the container. A name
that collides with one of the relocated directories is dropped rather than
forwarded — a bare `-e NAME` is read from the host and, coming later on the
command line, would otherwise beat the per-launch assignment.

**This changes nothing about the missing canary above.** Containment bounds the
blast radius of an unverified guard; it is not evidence the guard holds.
`KIMI_GUARD_VERIFIED` stays `false` until #8636's live receipt lands, contained
or not.

The *containment* mechanism itself — as opposed to the guard — has its own
live receipt:
[`kimi-containment-verification-2026-09-22.md`](kimi-containment-verification-2026-09-22.md)
records a real `docker build` + `docker/native/test-image.sh` pass, two
concurrent contained workers with disjoint `KIMI_CODE_HOME`s, and a
post-run writable-layer scan showing no credential value reached disk. It is
not a substitute for #8434 (still open, Pi/OpenCode-scoped) or for a
credentialed Kimi task run, which no account in this environment could
provide.

## Toolless launch detection

CLI exit zero is not acceptance evidence (see "Residual limits"), and since
#8448 it is no longer *treated* as evidence either. The 2.x canary above and its
passing 1.x control both exited `0`; nothing in the exit status distinguished
"did the work with four guarded tools" from "had no tools and did nothing".

A guarded native role tick is therefore classified from its own native event
stream, not its exit code (`loom-daemon/src/role_runner/toolless_launch.rs`,
built on `worker_spawn::launch_outcome`). A tick that exits 0 with **zero
`loom_*` tool uses** in its own region of `.loom/logs/role-<role>.log` is
reported as a `Failure`, counted and escalated like any other failed tick,
rather than as a healthy `Success`. A tool call that the guard *denied* still
counts as a use — the binding loaded, policy then said no; that is a working
guard, not a toolless launch.

The check deliberately stands down — leaving the pre-#8448 success verdict
untouched — for any non-native runtime, a tick with no resolved runtime
admission, a log whose per-tick anchor is missing, and a stream containing no
events this classifier can parse. The last one matters most: an unparsed stream
is a gap in Loom's own observation, never evidence about the launch, so a future
harness release that renames its event types degrades the check to "no opinion"
instead of failing every tick.

Free-form trials without a Loom role retain the harness's ordinary tools.
The guarded tool contract applies to role-tagged launches and `/loom:<role>`
invocations, including the full sweep. Direct interactive `pi` / `opencode`
sessions remain user-controlled and are not automatically confined by Loom.

## Control capability and lifecycle

`loomControl` means the runtime can execute the installed role's filesystem,
forge and Loom-helper workflow. It is independent of MCP transport. Builder
and Doctor require `loomControl` plus `worktreeIsolation`; Judge requires
`loomControl`. Claude and Codex retain their existing control route. Aider's
unverified generic adapter remains unadmitted for these roles. A custom runtime
must declare the new capability only after verifying its control route.

Pi/OpenCode keep `mcp: no` and `subagents: no`: these adapters neither provision
an MCP server nor expose unverified delegation. `hooks: partial` is deliberate:
the tool-policy boundary is implemented, but Claude Stop hooks and its other
hook events are not supplied. Full native sweeps execute the role lifecycle
sequentially using [native sweep instructions](native-sweep.md) and the installed
role prompts, with the same issue claims, CI, review and merge helpers.

Upgrade the binary and resync installed defaults together. An older installed
role sidecar that still requires MCP will continue refusing Pi until resynced;
an older binary does not implement this tool service.

## Run issue work

With the current binary, installed defaults, and authenticated CLI:

```sh
LOOM_RUNTIME=pi loom-daemon spawn-worker -- --profile zai-flash \
  --log /tmp/issue-worker.log -p '/loom:sweep 123'
LOOM_RUNTIME=opencode loom-daemon spawn-worker -- --profile zai-flash \
  --log /tmp/pr-review.log -p '/loom:judge 456'
```

For daemon-driven work, set `runtimes.default` (or its `sweep-lifecycle` role
binding) to `pi` or `opencode`, and `runtimes.defaultModelProfile` to the desired
profile. Remove stale explicit Claude model pins when changing runtimes;
explicit incompatible pins fail rather than silently choosing another model.
Standalone scheduled roles can have different runtime bindings; one sweep uses
one runtime throughout. Keep the purchased provider plan's concurrency limit
in mind when setting the existing worker concurrency configuration.

## Reaping stale native launch state

A per-launch directory is never reused, and nothing used to remove it: three
fleet hosts accumulated 252–358 session directories (30–37 GB) in a day, and
one filled its root volume twice (#8663). There is no exit-time hook to use —
`worker_spawn::exec` replaces the Loom process image with the harness CLI, so
no parent survives the session — so the reap runs in the two places a Loom
process demonstrably exists:

1. **At the next launch for the same workspace**, before the new session
   directory is created. Best effort: a reap failure never fails a launch.
2. **`loom-daemon clean`**, over every workspace under the base. It *reports*
   by default (and under `--dry-run`) and removes with `--force`/`-y`, which is
   what the fleet's scheduled `clean --deep --safe -y` pass uses. `--safe` does
   not narrow it: a session directory has no PR to be merged, exactly like the
   log and tmux artifacts.

What is removed is decided by liveness first, age second. Each session
directory carries a `loom-session.json` record naming the harness pid (`execve`
preserves it) and the host that wrote it:

| Session state | Removed when |
| --- | --- |
| This host, pid alive | never — a 12-hour sweep is safe |
| This host, pid exited | 15 minutes old (covers the record-write race) |
| Another host's record, or no record (pre-#8663) | 6 hours old |
| Age unreadable | never |

Shared binding trees are content-keyed, so a plugin-pin bump strands the
previous one; each carries a `.last-used` marker refreshed per launch and is
removed after 7 idle days. Staging trees stranded by a crashed provision are
removed on the orphan threshold.

The equivalent manual sweep, for a host that cannot run `clean` (it has no
liveness check, so run it only when no native launch is in flight):

```bash
find ~/.local/state/loom/native-tools -mindepth 2 -maxdepth 2 -type d -mmin +360 -exec rm -rf {} +
```

## Residual limits

These are application policy guards, not an OS sandbox. They inherit the
existing shell-policy limits (arbitrary programs can have effects beyond what
a shell command analyzer understands), filesystem check/use races, and the
existing path-derived managed-worktree ownership limits. Trusted installed
hooks, helper binaries, harness plugins and configuration are executable code.
Do not treat this integration as containment of a malicious plugin or host.

OpenCode 2.x `run` attaches by default to a shared, long-lived background
service. The guarded binding reads `LOOM_WORKSPACE`, `LOOM_NATIVE_TOOL_BIN` and
its working directory from *whichever process loads the plugin*; under a shared
service those belong to the service — absent, so no tools, or stale from an
earlier launch, so tool calls are guarded against the wrong workspace. Loom
therefore always passes `--standalone` on 2.x. An operator who starts OpenCode
some other way, or points a worker at a shared server, is outside this boundary.

The initial guarded tool set deliberately excludes native task delegation,
MCP tools, interactive stdin, and unclassified plugin tools. It uses the same
small edit interface in both harnesses; this is not a benchmark of OpenCode's
full native editing/tool ecosystem. CLI exit zero is not acceptance evidence
(see "Toolless launch detection" for the check that now enforces this on role
ticks). Record failed attempts and independently verify code and forge outcomes.

An OS-level backstop for exactly those residual limits is available, opt-in, as
per-sweep ephemeral containment (`runtimes.containment.native: "ephemeral"`,
issue #8403) — the same container lifetime Claude sweeps use, with an image
pinning the CLI versions recorded above, per-launch XDG/config/session
directories, and env-only credential injection. It does not narrow the
application-policy limits described here; it bounds their blast radius. See
[runtime adapters](runtime-adapters.md) § "Native-harness ephemeral
containment". Uncontained dispatch remains the default and is unchanged.

The shared bridge's historical filename is `guard-codex-bridge.sh`; the Rust
boundary uses its established internal request/decision protocol, not a Codex
process or account. Future policy retirement should replace that shared service,
not introduce harness-specific policy copies.
