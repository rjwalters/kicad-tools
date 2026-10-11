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

**This table is narrative, not authoritative (#9597).** `fleet-config
render` classifies every changed dotted path itself, against a code-owned
table (`loom-daemon/src/fleet_store/reload.rs`) built on the exact same
mechanical test above — that table is what `render`'s output and
`loom-daemon status`'s "Pending restart:" line actually act on, so it cannot
silently drift the way the rows above already had (`autonomous.workFinder`'s
`maxConcurrent` / `maxConcurrentPerRepo` / `extraSkipLabels` all went live in
#9060/#9090/prior work without this table ever being updated to say so). If
this table and `reload.rs` ever disagree, `reload.rs` is correct; open an
issue to fix this table's prose.

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

## Fleet-store hosts: the render happens before the loops (#9596)

On a host with `fleet.repo` set, the "requires a daemon restart" column above
is satisfied *by the restart itself*, not by a separate manual step. The
daemon's own startup sync (`loom_daemon::fleet_sync`, see
[`daemon-reference.md`](daemon-reference.md) → "Automatic sync") fetches the
store and renders the machine and host-local tiers **before** any
`read_*_config` call that gates a loop — so a knob landed in the store is live
on the host's next daemon start, with no `fleet-config render` by hand and no
second restart to pick up what the first one rendered.

That does not retire the rule at the top of this file; it narrows what counts
as evidence. A store-landed config change is still not effective until a host
actually restarted and rendered it, and the observation that it did is now
directly available: `loom-daemon status`'s `Fleet store:` block names the store
commit each tier was rendered from, and the timer pass keeps reporting drift
between passes. A store commit with no host reporting it is exactly the
"landed != effective" gap, made visible instead of inferred.

The timer half is **detection, not convergence** by default: it reports drift
and writes nothing unless `fleet.autoApply` is on. A change that must take
effect without waiting for a restart still needs one.

## Where each kind of key lives (#11441)

Tier 1 (fleet-gitops `config.defaults` / `config.hosts.<h>`, rendered to the
machine-level `defaults.json`) is where **fleet policy** is decided. Tier 2/3
(`.loom/config.json`, `.loom-project/project.json`) are deep-merged **over** it
key by key, so a fleet key committed in a repo silently wins for that repo and
fleet-gitops alone no longer tells you what runs. The classification below is
the human view of `CLASSIFICATION` in `loom-daemon/src/fleet_policy.rs`, which
`loom-daemon validate`, CI and `loom-daemon status` all read.

| Class | Top-level keys | Lives in |
|-------|----------------|----------|
| **Fleet** | `autonomous`, `runtimes`, `safehouse`, `observability`, `offlineMode`, `guide`, `sweep`, `daemon`, `fleet` | fleet-gitops only |
| **Repo** | `terminals`, `buildGate`, `branchProtection`, `guards`, `labels`, `forge`, `reflection`, `version` | tracked `.loom/config.json` / `.loom-project/project.json` |
| **Host** | `worktree`, `health_monitoring` (and per-host paths, see below) | `.loom-local/local.json` |

Sub-key rows (#11451) — dotted paths classify with **longest prefix wins**
(a row only ever matches at a `.` boundary, so `forge.githubAppX` falls
through to `forge`), and a sub-key row carves a more specific class out of a
differently-classified parent:

| Class | Dotted path | Why |
|-------|-------------|-----|
| **Repo** | `autonomous.workFinder.extraSkipLabels` | documented per-repo knob (`work_finder.rs`, `cli/skip_labels.rs`, #6685) |
| **Fleet** | `forge.githubApp`, `forge.identities`, `forge.readPool` | fleet-shared forge connection policy; a repo-tier value silently re-points it for that repo |

Consequences of the sub-key walk: an `autonomous` holding only
`workFinder.extraSkipLabels` is fully carved out and produces no finding,
while `autonomous` that also sets any other knob (e.g. `roleRunner.onIdle`,
`workFinder.enabled`) is flagged once, at `autonomous`. A Repo parent
holding a Fleet sub-key is flagged at the sub-key (`forge.githubApp`).
An allow-list entry may name a dotted path, and a top-level entry covers its
sub-keys (an entry for `forge` allows a `forge.githubApp` override). A dotted
entry under a Fleet parent exempts just that subtree: with an entry for
`autonomous.roleRunner.onIdle`, an `autonomous` holding only that leaf passes,
while one that also sets an unallowed sibling (`workFinder.enabled`) is still
flagged at `autonomous`.

This supersedes the "Project (default) or Local" ownership of `autonomous.*`,
`safehouse.*` and `observability.*` in `docs/design/config-resolution-tiers.md`
section 3: a repo is not the place to decide fleet behaviour. A repo with no
`.loom/config.json` resolves entirely from tier 1 (the resolver soft-fails each
missing tier).

Enforcement:

- `loom-daemon validate --strict` (CI runs the narrower `validate . --fleet-policy-only`) exits 2 on a Fleet key present in
  a tracked repo tier (a `null` value counts: it clears the tier-1 value; dotted
  sub-key rows count too, per the table above) unless `.loom/fleet-policy-allow.json` has an entry
  `{"key": "...", "reason": "..."}` for it (exact dotted path, or an ancestor
  covering it). An entry with an empty reason is
  itself rejected. Use the allow-list only for a deliberate per-repo override.
- The install template (`defaults/config.json`) seeds no Fleet keys.
- **Upgrade-time strip (#11451)**: the reinstall merge (`loom-daemon init`,
  `loom update`) removes a Fleet key from an installed `.loom/config.json`
  **only when its committed value equals the old template seed exactly** —
  `offlineMode: false`, `runtimes: {"default": "claude"}`, or the pre-#11445
  `safehouse` block (the seeds live in `fleet_policy::legacy_template_seeds`).
  This changes no behaviour: the current template no longer ships those keys,
  so fleet policy resolves from tier 1 either way. Any other Fleet content is
  left in place, logged (`init: config.json: stripped legacy template seed
  …`), and reported by validate so the operator can migrate it or allow-list
  it with a reason. The strip is byte-idempotent — a second run is a no-op.
- `loom-daemon status` lists, per managed repo, each tracked-tier leaf
  classified Fleet (sub-key rows apply per leaf, so `forge.githubApp.*` is
  reported and `autonomous.workFinder.extraSkipLabels` is not) that differs
  from tier 1, with the fleet value and the effective value (a value equal to
  tier 1 is not reported).

Per-repo fleet report over the roster (#11451): check out each roster repo and
run the narrow validator; whatever it reports is the remaining set of per-repo
fleet-key overrides the operator decides on (keep it → allow-list entry with a
reason; drop it → delete and let the fleet value win):

```bash
# From a fleet-gitops checkout (repos.yml lists the roster as owner/name —
# adjust the yq path if the layout differs):
mkdir -p /tmp/fleet-audit && rm -rf /tmp/fleet-audit/*
for r in $(yq -r '.repos[] | select(.fleet) | .full_name' repos.yml); do
  short="${r#*/}"
  if git clone -q --depth 1 "git@github.com:$r" "/tmp/fleet-audit/$short"; then
    loom-daemon validate "/tmp/fleet-audit/$short" --fleet-policy-only \
      && echo "$r: clean" || echo "$r: FINDINGS (see stderr above)"
  else
    echo "$r: clone failed"
  fi
done
```

Migrating existing values into fleet-gitops is an operator step; until a repo's
values move, its keys are allow-listed with a reason pointing at the migration.

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

**Partially landed for `fleet-config render` specifically (#9597).** A
render that changes a live-reloadable path now confirms it against a
running daemon over IPC and prints so at render time, with nothing further
to check; a restart-required path is recorded and surfaces in
`loom-daemon status` until the daemon that saw the drift actually restarts —
closing the "silent staleness" half of #5963's gap for this one write path.
This does **not** change what is live vs. restart-required (that split is
unchanged, and still just as real) — it only makes the render command name
which bucket a change landed in instead of leaving the closer to work it out
by hand. Actually hot-reloading a knob currently marked restart-required (so
that bucket shrinks) is still the open, harder half of this follow-up.
