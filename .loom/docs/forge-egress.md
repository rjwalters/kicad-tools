# Forge egress policy (`loom-daemon forge egress`)

Answers one question for a process: **will its `gh` reach the mandated API
origin?** It is the Loom half of the 2am GitHub-egress contract ("Contract
4"; epic #9983, C1 #9984), implemented natively in `loom-daemon` (ADR-0018)
so a Loom host's routing verdict never depends on a sibling 2am checkout.

**No policy configured ⇒ every entry point is a no-op that exits 0.** Nothing
changes for upstream users or Gitea deployments.

## Verbs

| Command | What | Network |
|---|---|---|
| `loom-daemon forge egress assert [--quiet]` | policy + effective `gh` build + every effective `GH_CONFIG_DIR` profile | none |
| `loom-daemon forge egress doctor [--json]` | routing + git + runtime + telemetry sections, each with its own `exit_code` | only the configured negative canary |
| `loom-daemon forge egress policy` | the resolved policy: path, origin, ignored candidates, token-shaped values redacted | none |
| `loom-daemon forge egress guard --for-command <cmd>` | classify one typed Bash command for the `loom:forge-egress` hook rule (#9989): exit 1 + `BLOCKED [routing.denied-by-guard]: …` for a launcher bypass under an enforcing policy, else silent exit 0 | none |

**Exit taxonomy** (the process exit code is the **routing** verdict only):
`0` aligned, `1` findings, `2` verification incomplete. Both 1 and 2 fail
routing admission. A `schemaVersion` other than 1 is `2`, never `0`.

## Policy resolution

The first candidate that is present wins outright. Policies are never merged,
and the validator never falls back to a cached, default or narrower one:

1. `$LOOM_FORGE_EGRESS_POLICY` (`origin: env`). When it is set it is
   authoritative even if the file is missing. A missing file is
   `policy.unreadable`, exit 2.
2. `/etc/loom/forge-egress/policy.json` (`origin: machine`). Only a missing
   file is absent; one that cannot be stat'ed (e.g. `EACCES`) still wins and
   is `policy.unreadable`, exit 2.
3. `/etc/2am/github-egress/policy.json` (`origin: machine`) — the 2am
   deployment's path, probed after Loom's own with the same missing-vs-
   unreadable rule. This is the shared-discovery mechanism: a host provisioned
   for 2am's `scripts/github-egress.py` (same vendored schema) is found by both
   validators with no reprovisioning, so the two doctors cannot disagree.
4. `.loom/config.json` → `forge.egress.policyPath` (`origin: repo`). A relative
   path resolves against the repo root.
5. None ⇒ `unconfigured`.

**Unconfigured is visible, not silent** (#10168). With no policy, `doctor`,
`assert` and `status` report a `policy.unconfigured` finding ("GitHub routing is
neither enforced nor validated on this host"). On a generic install it is a
non-fatal `notice` (exit 0, daemon gate admits). A host declares itself
*managed* by setting `LOOM_FORGE_EGRESS_MANAGED=1` (also `true`/`yes`/`on`) or
by creating the marker file `/etc/loom/forge-egress/managed`; the same finding
is then `incomplete` and exits 2, matching 2am's doctor. The marker only adds
strictness; the daemon gate never refuses on an unconfigured host.

Lower-precedence candidates that were present are listed under
`policy.ignored`, so a repo-local policy can never weaken a machine one. A
repo-origin policy may not name a command: its `enforcement.negativeCanary` is
never run, and the runtime section reports `runtime.unverifiable`.

```json
{ "forge": { "egress": { "policyPath": "ops/forge-egress-policy.json" } } }
```

The document is 2am's `infra/github-proxy/policy.schema.json` v1, vendored
byte-identical at `loom-daemon/src/forge_egress/policy.schema.json`.

## Finding codes

These are the 2am validator's codes (`scripts/lib/github_egress.py`), so one
alert rule (loom-ui#1015) matches both validators:

- **routing**: `policy.schema`, `policy.schema-version`, `policy.unreadable`,
  `policy.inline-secret`, `policy.api-origin-port`,
  `policy.origin-equals-logical-host`, `ghhost.points-at-gateway`,
  `ghhost.unapproved`, `ghrepo.host-qualified-conflict`,
  `toolchain.below-api-host-floor`, `toolchain.unpinned-version`,
  `toolchain.gh-unresolvable`, `toolchain.launcher-not-first`,
  `toolchain.launcher-python3-missing` (Loom-only, container spawn),
  `apiconfig.profile-not-enumerated`, `apiconfig.shadowed-profile`
- **git**: `git.unqualified` (incomplete), `git.premature-rewrite`,
  `git.missing-rewrite`, `git.enforced-without-host`
- **runtime**: `runtime.unverifiable`, `runtime.verified-without-canary`
- **telemetry**: `telemetry.no-identity`, `telemetry.no-endpoint`

Loom-only codes cover surfaces that 2am's validator cannot see
(`forge_egress::checks::LOOM_ONLY_CODES`):

- `apiconfig.api-host-missing` / `apiconfig.api-host-mismatch`: a profile's
  `hosts.yml` logical-host entry lacks the API-host setting the policy
  requires, or names a different host. This covers Loom's token-only republication (scenario 17,
  #9986).
- `runtime.bypass-open`: the canary's direct request succeeded.
- `toolchain.policy-launcher-declined`: the policy's `launcherPath` exists,
  but the `gh` the daemon itself execs is neither that launcher nor `PATH`'s
  `gh` (which `toolchain.launcher-not-first` covers). Typically
  `$LOOM_GH_BIN` with `LOOM_GH_NO_POLICY_LAUNCHER=1`, or with a policy whose
  origin may not choose the executable (#9995).
- `telemetry.loom-exporter-not-otlp`: Loom's own observability config has no
  `otlp` exporter.
- `routing.denied-by-guard`: the `loom:forge-egress` `PreToolUse` rule denied
  a typed bypass of the managed launcher (raw HTTP to the GitHub API host, a `GH_HOST`
  override, `gh auth login`, a path-qualified `gh`, an SDK install, …). A denial code,
  never a report finding; see `guard-hooks.md` "Forge Egress Guard" (#9989).

The checked profiles are the process's `GH_CONFIG_DIR` (or `gh`'s default
directory when none is exported), plus every profile Loom publishes:
`.loom/gh-config` and `.loom/gh-config-by-owner/<owner>`. The effective `gh`
is the binary Loom will exec, picked by the `gh_invocation` resolver (first
hit wins):

1. `toolchain.launcherPath`, only from an **env**- or **machine**-origin
   policy (a repo-local policy never chooses the executable), and only when
   that file exists (#9995);
2. `$LOOM_GH_BIN`;
3. `gh` on `PATH`.

`LOOM_GH_NO_POLICY_LAUNCHER=1` declines rung 1. Every test harness that stubs
`gh` sets it, so a host's policy launcher never outranks the stub. It is not
a silent bypass: env already outranks the machine policy, and the checks
report where a declined rung lands. The version floor measures the exec
target. A landing on bare `gh` is `toolchain.launcher-not-first`. A landing
on `$LOOM_GH_BIN`, or on anything else that is neither the existing launcher
nor `PATH`'s `gh`, is `toolchain.policy-launcher-declined`. All three are
routing findings, so under `enforcement.api = required` `assert` fails.

The version floor measures that effective `gh` (`observed.ghPath`; the rung
that won is `observed.ghSource`). `toolchain.launcher-not-first` measures the
`gh` an agent's plain `gh` resolves to: bare `gh` on `PATH`, never the
resolver (`observed.pathGhPath`). `gh --version` is probed with no token
variables and an empty config directory, because `gh` can make a live call
even for `--version`.

**Never in any output:** `hosts.yml` contents, tokens, credential-helper
output, git rewrite URLs or the environment. Reports carry paths, hosts,
versions, counts and remedies only. Token-shaped strings are redacted.

## Where it runs

| Entry point | Call | On failure under `enforcement.api = required` |
|---|---|---|
| daemon startup, then every `LOOM_FORGE_EGRESS_DOCTOR_INTERVAL_SECS` (default 900) | `doctor` | logs each finding + repair command, caches `~/.loom/forge-egress-doctor.json`, publishes `forge.egress.drift` when the code set changes; the daemon stays up |
| `loom-daemon status` / `status --json` (`forge_egress` key) | fresh `assert` + cached daemon `doctor` | shows the codes and fixes; shows the `policy.unconfigured` notice when unconfigured |
| sweep dispatch | `assert` | refuses before any claim or spawn; event `sweep.blocked` with `reason: forge-egress` |
| worker spawn (`spawn-worker.sh` → `loom-daemon spawn-worker`) | `assert` | does not spawn (exit 78) |
| `install_self_check` (`forge-egress-aligned`) | `assert` | files an issue naming the codes, refreshes it when the codes change, and closes it once aligned or once no policy is configured |
| `loom-daemon init` (`install-loom.sh`, `loom update`) | `doctor` | prints the findings; non-zero exit |
| `resync-installed.sh` | `doctor` | prints the findings after the sync completes; exits with the doctor's code (non-zero only under `required`, or 2 on an unconfigured host declared managed); `--dry-run` never runs it and never fails; a daemon without `forge egress` warns |
| `/loom:sweep` pre-wave hygiene | `assert` | advisory text in the summary |
| `PreToolUse` Bash hook (`guard-loom-workflow.sh`, also under Codex via `guard-codex-bridge.sh`) | `guard --for-command` | denies the typed bypass naming `routing.denied-by-guard`, plain `gh …` and the policy origin; opt-out `guards.forgeEgress=false` (#9989) |

`enforcement.api = observe` logs the same findings and proceeds. An unreadable
policy, or one with an unknown `schemaVersion`, is never treated as observe-only.

## The managed `gh` launcher (Loom consumes it; it does not provision it)

On a policy-governed host every `gh` is 2am's managed launcher
(`scripts/gh-managed.py`, 2am#2002), provisioned as an executable named
`gh` at `toolchain.launcherPath`. Provisioning it is host/image/CI work
(2am#1931 / #1928); Loom ships no second implementation of that security
boundary. To use it, point `toolchain.launcherPath` at the provisioned file (and
`toolchain.upstreamGhPath` at the pinned upstream `gh`) in an env- or
machine-origin policy. A repo-origin policy never chooses an executable.

What Loom does with `launcherPath` (all no-ops with no policy):

- **Daemon `gh`** — the resolver's first rung (see `NO_POLICY_LAUNCHER_ENV`).
  That opt-out affects only which `gh` the daemon execs: worker and container
  credential admission ignores it, so it can never turn a `required` or
  managed-marker host into `unconfigured` (#10446).
- **Daemon `PATH`** — `loom-daemon-start.sh` (`daemon-start`) puts the launcher's
  directory first on the `PATH` it bakes into the launchd plist / systemd unit
  (also ahead of a `LOOM_DAEMON_PATH` override), so scripts, hooks and role
  prompts the daemon runs resolve the launcher as `gh`; so does the fleet
  drain's local `gh`. The directory comes from the policy, never from the
  canonical PATH constant. Re-render (restart) after changing `launcherPath`.
- **Bare-metal workers** — `loom-daemon spawn-worker` puts the launcher's
  directory first on the worker `PATH` (ahead of the `gh-cached` front, which is
  therefore bypassed under a policy). Under `enforcement.api = required` a worker
  whose first `gh` is not the launcher is not spawned (`toolchain.launcher-not-first`).
- **Containers** (`spawn-claude.sh`, native containment) — the launcher directory,
  `toolchain.upstreamGhPath`, the policy file and the `principal.credentialRef`
  file (`file:/abs/path`) are mounted read-only at their host paths, and
  `LOOM_FORGE_EGRESS_POLICY` / `GITHUB_EGRESS_POLICY` name the policy. The
  whole launcher directory is mounted, so give `launcherPath` a dedicated
  directory: a shared one such as `/usr/local/bin` would shadow the image's
  copy (where `docker/worker/Dockerfile` installs `loom-daemon`) and expose
  its siblings. `~/.config/gh`
  is not mounted and `GH_TOKEN` / `GITHUB_TOKEN` are not forwarded. The launcher
  is Python 3: under `required`, an image without `python3` is refused
  (`toolchain.launcher-python3-missing`) and so is one whose `gh` does not
  resolve to the launcher (`toolchain.launcher-not-first`) — on both container
  paths (`spawn-worker` and `spawn-claude.sh`, the latter via `loom-daemon forge
  egress container-args --image <image>`, exit 78). The policy is schema-validated
  before any routing result: an unreadable policy, an unsupported
  `schemaVersion`, or — unless it is a valid `observe` policy — a schema finding
  or a missing/empty launcher is refused with exit 78 and never falls back to
  `~/.config/gh` / `GH_TOKEN`. `observe` logs those findings and proceeds.
  `loom-daemon forge egress container-args` makes the whole container
  credential decision: an explicit status line first (`loom-forge-egress:
  managed` / `unconfigured` / `observe-unmanaged`), then the docker arguments —
  the managed mounts, or only for the last two the legacy token-by-name /
  `~/.config/gh` arguments. `spawn-claude.sh` appends them; empty output, a
  failure or a missing status line refuses (78), never empty-means-none.
- **Exit codes** — a launcher exit of `78` is `outcome=routing_blocked` and `69`
  is `outcome=adapter_unavailable`. Neither is a forge answer: the invocation
  surfaces as unavailable (never an empty result) and is not retried.

## Lockstep with 2am

`loom-daemon/tests/fixtures/forge-egress/scenarios.json` is shared data. Each
row is a static validator input with `expected` (Loom) and `upstream` (2am)
codes per section. Rows `s00`–`s17` translate 2am's 18 live-`gh` scenarios
(`scripts/tests/test-github-egress-fixtures.py`, numbered 0–17), and the
`u-*` rows mirror its unit tests. `cargo test -p loom-daemon --test
forge_egress` checks every row. Each upstream code must appear in 2am's
order, and each extra code must be Loom-only. A scenario added in either repo
lands as a row in both. When 2am changes, re-derive `upstream` by running its
validator over the rows and record the commit in `upstream_validator`.
