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
3. `.loom/config.json` → `forge.egress.policyPath` (`origin: repo`). A relative
   path resolves against the repo root.
4. None ⇒ `unconfigured`.

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
- `telemetry.loom-exporter-not-otlp`: Loom's own observability config has no
  `otlp` exporter.

The checked profiles are the process's `GH_CONFIG_DIR` (or `gh`'s default
directory when none is exported), plus every profile Loom publishes:
`.loom/gh-config` and `.loom/gh-config-by-owner/<owner>`. The effective `gh`
is `$LOOM_GH_BIN`, else `gh` on `PATH`, meaning the binary Loom will exec.
`gh --version` is probed with no token variables and an empty config
directory, because `gh` can make a live call even for `--version`.

**Never in any output:** `hosts.yml` contents, tokens, credential-helper
output, git rewrite URLs or the environment. Reports carry paths, hosts,
versions, counts and remedies only. Token-shaped strings are redacted.

## Where it runs

| Entry point | Call | On failure under `enforcement.api = required` |
|---|---|---|
| daemon startup, then every `LOOM_FORGE_EGRESS_DOCTOR_INTERVAL_SECS` (default 900) | `doctor` | logs each finding + repair command, caches `~/.loom/forge-egress-doctor.json`, publishes `forge.egress.drift` when the code set changes; the daemon stays up |
| `loom-daemon status` / `status --json` (`forge_egress` key) | fresh `assert` + cached daemon `doctor` | shows the codes and fixes; prints nothing when unconfigured |
| sweep dispatch | `assert` | refuses before any claim or spawn; event `sweep.blocked` with `reason: forge-egress` |
| worker spawn (`spawn-worker.sh` → `loom-daemon spawn-worker`) | `assert` | does not spawn (exit 78) |
| `install_self_check` (`forge-egress-aligned`) | `assert` | files an issue naming the codes, refreshes it when the codes change, and closes it once aligned or once no policy is configured |
| `loom-daemon init` (`install-loom.sh`, `loom update`) | `doctor` | prints the findings; non-zero exit |
| `/loom:sweep` pre-wave hygiene | `assert` | advisory text in the summary |

`enforcement.api = observe` logs the same findings and proceeds. An unreadable
policy, or one with an unknown `schemaVersion`, is never treated as observe-only.

Not wired yet: post-publication `assert` + rollback (C3, #9986) and
`resync-installed.sh` (#9996; run `loom-daemon forge egress doctor` after a
resync by hand). The facade resolver's policy-launcher rung is #9995.

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
