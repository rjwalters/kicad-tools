# Fleet-config changes: "landed != effective" (#5963)

## The rule

**A fleet-config or fleet-behaviour change may only close once its effect is
observed on a live host — not when the file lands.** "Merged to `main`" and
"issue closed COMPLETED" both mean the *source* changed; neither means a
*running daemon* changed. Several of the fleet's own caching layers make that
gap invisible by default (see below), so the closing evidence for such a
change must include one of:

- the changed behaviour **observed running on a host** — a log line, a
  `status`/`calibrate` field, a produced artifact, a role actually firing; or
- an explicit note that the change requires a **daemon restart**, a
  **`resync-installed.sh`**, or a **per-host install/reload step**, **and
  that step has been performed and verified on each affected host** (not just
  scheduled or assumed).

A closed issue that reads as done but changed nothing on the hosts is worse
than an open one — closing turns off the only thing that was still watching
it.

## Why this exists

This convention was written after the same shape recurred four times in one
window, each caught only by an out-of-band check hours to days later:

- **loom#5846** — an operator-only sub-kind rule landed in role prompts under
  `defaults/`; installed surfaces (`.loom/roles/`, `.claude/commands/loom/`,
  …) are copied at **install time**, so a `git pull` alone never refreshed
  the copies actually executed — needed `resync-installed.sh`.
- **loom#5874** — a prompt change shipped with no `VERSION` bump, so every
  "is the fleet current" currency check kept reporting green while behaviour
  differed host to host.
- **example-org/fleet-repo#302** — a scheduled cleaner timer was installed recording an
  interpreter it would itself refuse to run under — "installed" but doomed to
  fail on every fire.
- **example-org/fleet-repo#303** — the sharpest case: `doctor` was added to the
  role-runner default set, the PR closed COMPLETED, and the machine-level
  `defaults.json` on disk genuinely contained the change — yet zero doctor
  activity appeared on any host for 2.5 hours, until a manual daemon restart,
  because the process that would apply it was still the pre-change one.

None of these were wrong fixes. All four were correct changes that read as
"done" while producing zero effect on the fleet they were meant to change.

## What actually caches, in *this* daemon (checked against code, not folklore)

"Config lands on disk → picked up automatically" is true for some knobs and
false for others in `loom-daemon` itself — the split is not obvious from the
outside, which is exactly how #5963's motivating incident happened. As of
this doc, verified against source:

| Surface | Reload behaviour | Evidence |
|---|---|---|
| `autonomous.roleRunner.*` (`enabled`, `roles`, `onIdle`, `model`, …) | **Live** — re-read from `.loom/config.json` every role-runner tick, per registered root | `read_role_runner_config` is called inside the per-tick loop body (`loom-daemon/src/role_runner.rs`), not cached at spawn |
| `autonomous.workFinder.maxConcurrent` | **Requires a daemon restart** — the *operator ceiling* is resolved once at bring-up and frozen; only the `disk`/`ram` headroom terms of the per-tick `min(...)` are recomputed live around it | `resolve_max_concurrent_with_config` is called once in `loom-daemon/src/daemon_service.rs` (outside any loop) and the resulting `configured_max` is passed into `resolve_dynamic_max_concurrent(disk, ram, configured_max)` in the tick body of `loom-daemon/src/work_finder.rs` |
| `autonomous.workFinder.enabled` (the work-finder **master switch**) | **Requires a daemon restart** — read once when the daemon decides whether to spawn the loop at all | `loom-daemon/src/daemon_service.rs` (`read_work_finder_config` called once, outside any loop, gating the `tokio::spawn`) |
| `autonomous.workFinder.maxAdmissionsPerTick` | **Requires a daemon restart** — resolved once at startup, the same startup-capture pattern as `maxConcurrent` | `loom-daemon/src/work_finder.rs` doc comment: "an operator retuning it takes effect on the next daemon restart, exactly like `configured_max` today" |
| `autonomous.workFinder.saturationBrake.*` (admission brake) | **Requires a daemon restart** — resolved once and registered as a process-global singleton alongside the host breaker | `loom-daemon/src/daemon_service.rs`, same startup block as `hostBreaker` below |
| `autonomous.hostBreaker.*` | **Requires a daemon restart** — resolved once at startup, registered as a process-global handle | `loom-daemon/src/daemon_service.rs`: "resolve its config once at startup ... and register the process-global handle" |
| `autonomous.rateLimitBreaker.*` | **Requires a daemon restart** — resolved once at startup, before any loop is spawned | `loom-daemon/src/daemon_service.rs`, registered unconditionally ahead of the work-finder branch |
| `autonomous.mainHealthGate.suppressDispatchDuringGate` | **Requires a daemon restart** — resolved once at startup from the primary workspace config | `loom-daemon/src/daemon_service.rs` comment: "Resolved once at startup from the same primary workspace config as the gate's master switch" |
| `autonomous.transcriptIngest.*` (`enabled`, `intervalSecs`, `windowHours`) | **Requires a daemon restart** — resolved once before the ingestion thread is spawned, then frozen for the life of that thread | `read_transcript_ingest_config` + `resolve_settings` are called inside `try_init_transcript_ingest` (`loom-daemon/src/activity/transcript_ingest.rs`), which `daemon_service.rs` invokes once at bring-up; the spawned thread's `loop` body reads only the already-resolved `interval`/`window_hours` locals |

The full, authoritative per-knob table (env override, default, notes) lives
in [`daemon-reference.md`](daemon-reference.md) → "Config surface
(`.loom/config.json → autonomous`)" — the rows above that require a restart
are annotated there too, at the point of definition, per this doc's
convention. **When in doubt about a knob not listed above, check whether its
`read_*` call sits inside a `loop`/tokio interval body (live) or is called
once during daemon bring-up before any loop starts (restart-required)** —
that is the actual mechanical test; "it's in `autonomous.*`" is not enough to
predict which bucket a given knob falls into.

## Other known caching surfaces (not daemon-config, same failure shape)

- **Installed prompt/role/doc/script surfaces** (`.loom/roles/`,
  `.claude/commands/loom/`, `.loom/hooks|scripts|docs|bin/`) are copied from
  `defaults/` at **install time only** — a `git pull` on `main` never
  refreshes them. Fix: `./.loom/scripts/resync-installed.sh` from the main
  checkout. Full detail: [`troubleshooting.md`](troubleshooting.md) →
  "Keeping installed `.loom/` copies fresh after a pull".
- **Compiled-in role/behaviour defaults** — e.g. which roles form the
  role-runner's *interval-default subset* when `autonomous.roleRunner.roles`
  is absent from config (`DEFAULT_ROLES` and friends in
  `loom-daemon/src/role_runner.rs`) is a **Rust constant**, not a config
  value. Changing it requires a new `loom-daemon` **binary** to actually be
  running on the host — a rebuild *and* a restart (or the autonomous
  self-update loop's settle window, see "Self-update" in
  [`daemon-reference.md`](daemon-reference.md)), not merely an edit to a JSON
  file. A committed `defaults.json`/`.loom/config.json` edit that only
  *pins* an explicit `roles: [...]` array is data and reloads live per the
  table above; changing what the *unset-key fallback* resolves to is a code
  change and does not.
- **Scheduled OS timers** (launchd plists, systemd user units) are
  **regenerated and reloaded**, not hot-edited — `bootout`/`bootstrap` on
  macOS, `daemon-reload` on Linux — see "macOS session-bootstrap hazard" and
  "systemd user unit" in [`daemon-reference.md`](daemon-reference.md). A
  timer definition changed on disk without the reload step keeps running the
  old one indefinitely.

## Applying the rule

Any role about to close an issue, or merge/label a PR `Closes #N`, whose
change is a fleet-config or fleet-behaviour change (touches
`.loom/config.json`'s `autonomous.*` block, a machine-level `defaults.json`,
an installed prompt/role/doc surface, or a scheduled timer definition) checks
for one of the two evidence forms in "The rule" above before treating it as
done. This is a per-occurrence judgment call today, the same way the
`loom:operator-only` sub-kind discipline is — there is no mechanical gate
that blocks a close lacking this evidence yet. Building one (a Judge/Champion
checklist item, or a bot that requires a linked host observation before
auto-closing a fleet-config issue) is deliberately left as follow-up, not
attempted here.

## Host-specific absolute paths never belong in the tracked tier (#6504)

A related, distinct failure shape: `.loom/config.json` is **tracked and
shared**, but some keys are inherently true of exactly one host — a
filesystem path, a socket, an on/off switch that depends on how that
specific machine is provisioned (`safehouse.socket`, `observability.ingestKeyFile`,
`worktree.root`). Committing one of these bakes one host's local truth into
every other host's `git pull`, and it has recurred repeatedly: #5354 and
#5464 each fixed one committed instance reactively, and #6499 showed the
sharpest failure mode — an abandoned `git stash pop` left live conflict
markers straddling a host's local patch and the tracked file, which then got
**committed**, making `.loom/config.json` invalid JSON and silently dropping
`observability`/`safehouse`/`autonomous.roleRunner` config to built-in
defaults for ~70 minutes.

**The fix is structural, not a one-time cleanup:**

- **The documented home for a host-specific value is the gitignored
  `.loom-local/local.json` tier** (or, for the subset of knobs that already
  have a correct `$HOME`-relative built-in default — `observability.ingestKeyFile`
  is the example — simply not committing a value at all and letting the
  default resolve per host). Both tiers are read through the same
  `resolve_effective_config`/`loom_resolve_config` chain documented in
  [`docs/design/config-resolution-tiers.md`](https://github.com/rjwalters/loom/blob/main/docs/design/config-resolution-tiers.md),
  which also carries §5's step-by-step runbook for moving an already-dirty
  key onto the local tier by hand.
- **A CI guard rejects a newly-committed offender** before it lands:
  `defaults/scripts/check-config-no-host-paths.sh` scans every tracked
  config tier (`.loom/config.json`, `.loom-project/project.json`) for any
  string value that is an absolute path under a home directory
  (`/home/<user>/…`, `/Users/<user>/…`, `/root/…`), independent of which key
  carries it — the structural counterpart to the conflict-marker gate
  (`check-conflict-markers.sh`, #6499) that stops the *other* half of this
  failure class (a corrupted, unparseable tracked file). A key that is
  genuinely repo/host-specific by design (`daemon.delegatedTo`, #5345) is
  allowlisted by exact key, not by loosening the path pattern.

This complements, but is distinct from, "landed != effective" above: a
host-specific path committed to the tracked tier is not a caching problem —
every host that pulls it sees the *wrong* value immediately and consistently,
not a stale one.

## The real fix, left as follow-up

The convention above is a guardrail, not a cure — it depends on every closer
remembering to check. The actual fix is to stop caching what should be live:
hot-reload `defaults.json` / role-runner roles on a tick (the way
`autonomous.roleRunner.*` already does for the *repo-level* config, per the
live/restart-required split above) instead of only at daemon start, for the
knobs the table marks "requires a daemon restart." That would retire this
convention for the daemon-config half of the problem entirely — tracked
against #5963, deliberately not attempted in the same change that documents
the guardrail it would replace.
