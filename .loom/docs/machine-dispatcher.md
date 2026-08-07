# Machine-level `loom` dispatcher

Epic #3835 Phase 3a (#4157) + Phase 3b (#4229). The `loom` dispatcher is a
machine-level entry point installed to `~/.local/bin/loom`. It resolves the
machine-level Loom checkout at `~/.local/share/loom` and exec's into it. It is a
sibling of the `~/.local/bin/loom-daemon` binary — one install per machine,
shared across every repo Loom is installed into.

```
loom <command> [options]
```

| Command | What it does |
|---------|--------------|
| `start` | Start the machine-level `loom-daemon` (delegates to `loom-daemon-start.sh`) |
| `stop`  | Stop the machine-level `loom-daemon` (delegates to `loom-daemon-stop.sh`) |
| `restart` | Restart the machine-level `loom-daemon` (supervised in-place relaunch, **not** a drain; falls back to stop+start) |
| `status`| Show machine-level + current-repo status (read-only) |
| `sweep <issue>` | Dispatch `/loom:sweep <issue>` for the current repo |
| `update`| Refresh the user-scoped mcp-loom bundle (#4230), then thin-delegate the daemon update to `loom-daemon-update.sh` |

Environment: `LOOM_HOME` overrides the machine-level checkout location (default
`~/.local/share/loom`).

## Checkout resolution — link, not a second clone (AC1)

The installer always runs **from** a Loom source checkout (`$LOOM_ROOT`). Rather
than cloning a *second* copy into `~/.local/share/loom` — which a developer
running Loom *on* the Loom repo could then let drift out of sync — provisioning
establishes the machine checkout as a **symlink**:

```
~/.local/share/loom -> $LOOM_ROOT
```

A symlink cannot diverge, so the hard AC1 constraint ("a developer running Loom
on the Loom repo must not end up with two divergent copies") holds by
construction. If `~/.local/share/loom` already exists as a **real directory**
(an operator's pre-existing standalone clone), provisioning leaves it untouched
— that is the supported "fresh clone" resolution. The `loom` dispatcher resolves
the checkout at runtime and works with either shape.

## The name collision with `./.loom/bin/loom`, and how it is resolved (AC3)

There are two different Loom surfaces that both answer to the name `loom`:

| Surface | What it is | Verbs |
|---------|-----------|-------|
| `~/.local/bin/loom` (this dispatcher) | Machine-level runtime driver | `start stop restart status sweep update` |
| `./.loom/bin/loom` | Per-repo **tmux agent-pool** manager | `start status health stop attach send scale logs` |

Three verbs collide by name — `start`, `stop`, `status` — and mean *different
things* in each surface.

**Why there is no PATH-shadowing.** `.loom/bin` is **never** added to `PATH`
anywhere in the tree, and every in-repo invocation of the pool manager is
path-qualified (`./.loom/bin/loom …`). A path-qualified call never resolves
through `PATH`, and a bare `loom …` on `PATH` never resolves to `./.loom/bin/loom`.
So the two invocation forms are **disjoint by construction** — adding
`~/.local/bin/loom` cannot shadow the pool manager, and the pool manager cannot
shadow the dispatcher. This is the `#4079` failure mode (a stale entry shadowing
another on `PATH`) *not* recurring; it is asserted by a regression test, not
patched with a compatibility shim.

**The residual, human-facing risk** is narrower: an operator typing a bare
`loom start` *while inside a consumer repo* might mean the tmux pool but get the
machine dispatcher. The dispatcher resolves this by **detecting** a nearby
`./.loom/bin/loom` (walking up from `$PWD`) and, for the three colliding verbs:

- **`start` / `stop`** (they mutate process state): the dispatcher **refuses**
  and prints a disambiguation naming *both* surfaces, then exits non-zero — it
  never silently runs the wrong one. Force the machine surface with
  `loom start --machine`, or run the pool with `./.loom/bin/loom start`.
  `restart` (#4229) gets the **same guard**, even though the per-repo pool
  manager has no `restart` verb of its own — guard consistency across the
  three process-mutating verbs is cheaper than explaining why `restart` is the
  odd one out.
- **`status`** (read-only, and required by AC7 to produce output from inside a
  repo): the dispatcher prints machine-level status and a clearly-labelled line
  pointing at the per-repo pool manager (`… run: ./.loom/bin/loom status`). It
  is never silent about the other surface.

## `status` output across contexts (AC7)

`loom status` reports a `repo:` line that distinguishes the three contexts:

- **consumer repo root** → `repo: consumer-repo (root: …)`
- **git worktree** (under `.loom/worktrees/…`) → `repo: git-worktree (main checkout: …)`
- **non-repo directory** (e.g. `/tmp`) → `repo: non-repo (no .loom/ found from cwd)`

## Config resolution (AC5)

`loom status` resolves configuration through the Phase 2 tier resolver
(`defaults/scripts/lib/config-resolver.sh`: private defaults → `.loom/config.json`
→ `.loom-project/project.json` → `.loom-local/local.json`) rather than reading
`.loom/config.json` directly, so a value overridden in `.loom-local/local.json`
wins. In a **non-repo** directory only the private-defaults tier contributes
(graceful degradation, not an error). When `jq` is unavailable the dispatcher
says so explicitly — it does **not** present a `jq`-less host as "no config".

## `update`: mcp-loom bundle refresh + thin daemon delegate

`loom update` does two things, in order:

1. **Refreshes the user-scoped mcp-loom bundle** (#4230, epic #3835 Phase 3c).
   It rebuilds `~/.local/share/loom/mcp-loom/dist/index.js` when the bundle is
   missing or stale (older than any file under `mcp-loom/src/`), so the one
   user-scope `loom` MCP server that serves **every** repo picks up new tools —
   the #3803 stale-dist drift fix. This step is best-effort: a build failure
   warns but never blocks the daemon update. It is skipped on `--check` /
   `--dry-run` and on a consumer-repo checkout that does not ship `mcp-loom/`.
2. **Delegates the daemon update** to the **existing** `loom-daemon-update.sh`
   (built by #3968, extended by the shipped #4055 self-update loop). The daemon
   half stays thin — the dispatcher implements **no** cargo rebuild / reprovision
   / restart logic of its own, so it neither pre-empts #4017 nor duplicates
   #4055. (The mcp-loom refresh lives in the dispatcher, not in
   `loom-daemon-update.sh`, because that delegate is daemon-scoped and
   short-circuits when the binary is already up to date — which would skip the
   bundle refresh on an mcp-loom-only roll.)

## User-scoped `loom` MCP server (#4230, epic #3835 Phase 3c)

The `loom` MCP server is registered **once per machine at user scope**, not
per-repo:

```
claude mcp add --scope user loom -- node ~/.local/share/loom/mcp-loom/dist/index.js
```

`scripts/install-loom.sh` does this at install time (idempotently — it removes
any existing user-scope entry first). A single user-scoped instance serves every
repo because mcp-loom resolves the **invoking** repo from its process CWD
(`getWorkspacePath()` in `mcp-loom/src/shared/config.ts`): `LOOM_WORKSPACE` env
override → walk up from `process.cwd()` to a `.loom/`/`.git` repo root (worktree
CWDs resolve to the main checkout via the git common dir, mirroring
`resolve_mcp_workspace()` in `claude-wrapper.sh`) → **loud failure** if no repo
root is found (there is deliberately **no** silent `~/GitHub/loom` fallback —
under user scope that would silently operate on the wrong repo).

### Why per-repo `.mcp.json` generation is demoted, and the shadowing hazard

Claude Code MCP scope precedence is **local > project > user**. A lingering
project-scope `loom` entry in a repo's `.mcp.json` therefore **outranks** the
user-scope server and would silently pin a stale per-repo bundle forever — the
#3803 drift class reborn as a *shadowing* drift. So:

- `scripts/setup-mcp.sh` is **demoted**: it no longer emits a `loom` entry, and
  it **strips** any pre-existing project-scope `loom` entry from the target's
  `.mcp.json` (removing the file if `loom` was its only server). Its residual
  role is **safehouse only** — when `safehouse` is enabled it emits a
  `.mcp.json` containing just the per-repo `safehouse` server (whose socket +
  persona are inherently per-repo/session; the per-worker persona is still
  injected at spawn time by `spawn-claude.sh --mcp-config`, unchanged).
- The `.mcp.json` symlink step in `worktree.sh` / `pr-worktree.sh` becomes
  **vestigial** under user scope (a user-scope server applies from any CWD) but
  is left intact — it is harmless and still serves un-migrated / safehouse repos.

### `claude-wrapper.sh` behavior with no `.mcp.json`

Both `claude-wrapper.sh` gates key off the project `.mcp.json`; under user scope
there is none, so their behavior was verified/adjusted (#4230):

- **Pre-flight** (`check_mcp_server`): an absent `.mcp.json` is a **non-fatal
  skip** (it always was) — now documented as the *expected* user-scope state.
  The per-session bundle-staleness gate that skip removes moves to `loom update`.
- **Connect gating** (startup monitor): previously, an absent `.mcp.json` fell
  into the conservative "can't enumerate project MCPs → kill the session" branch,
  which would wedge every migrated repo in a restart loop. It now recognizes the
  no-`.mcp.json` + loom-connected case as **healthy** and continues.

> **Debugging note (#4043):** if an MCP *tool* appears to hang during
> verification, that is the known unary-bridge framing bug in `daemon.ts`
> (`#4043`), not a registration fault — the CLI path is preferred for daemon
> control today. This change only moves *where the server is registered from*.

## Machine mode: LOOM_MACHINE_CHECKOUT hand-off (Phase 3b, #4229)

Phase 3a shipped `start`/`stop`/`update` as delegates, but each of the three
lifecycle scripts (`loom-daemon-start.sh`, `-stop.sh`, `-update.sh`) still
resolved its own operating root by walking up from `$PWD`
(`find_repo_root()`), independent of what this dispatcher had already
resolved. That produced two concrete gaps, closed here:

1. **`loom update` failed outside a Loom source checkout.** From a consumer
   repo, `find_repo_root()` found the consumer repo (no
   `loom-daemon/Cargo.toml` there) and refused; from a non-repo directory it
   found nothing at all and refused with "Not in a Loom workspace" — even
   though this dispatcher had *already* resolved and validated the machine
   checkout.
2. **`loom start`/`stop` bound machine-global daemon state to whichever repo
   they were invoked from.** The launchd label (`com.rjwalters.loom-daemon`)
   is a machine-wide singleton, but the rendered plist's `WorkingDirectory`
   and the `.daemon.pid`/`.daemon.flags` files were `$REPO_ROOT`-relative — so
   `loom start` from repo A and `loom update` from repo B could read/write two
   different pid/flags files against the same launchd job.

**The fix**: every verb that delegates into the checkout (`start`, `stop`,
`update`, `restart`) exports `LOOM_MACHINE_CHECKOUT=<resolved checkout>` before
exec'ing/invoking its lifecycle-script delegate. Each lifecycle script now
checks this variable *first*, ahead of its `$PWD`-based `find_repo_root()`
fallback:

- **Set** (machine mode — always true for a dispatcher-driven invocation): the
  checkout is used as the operating root (plist `WorkingDirectory`, the
  `loom-daemon/Cargo.toml` rebuild target for `update`) **regardless of
  `$PWD`**, and runtime artifacts — `.daemon.pid`, `.daemon.flags`, the
  startup log — resolve under `$HOME/.loom` (the pid/flags decision below),
  not under the checkout or the invoking directory.
- **Unset** (direct invocation of a lifecycle script, no dispatcher — the
  pre-#4229 dev workflow): every script behaves **byte-for-byte** as before,
  `$PWD`-based `find_repo_root()` included. Machine mode is strictly additive.

### The pid/flags relocation decision

`#4042` already established that a `.loom/.daemon.pid` file is an unreliable
running-state source under launchd (`KeepAlive:{SuccessfulExit:true}` assigns
a fresh pid on every supervised relaunch) — which argued for dropping pid
files entirely under launchd and treating `launchctl print` as the sole
source of truth. This unit takes the **narrower, lower-risk option** instead:
relocate `.daemon.pid`/`.daemon.flags`/the startup log from
`$REPO_ROOT/.loom/` to `$HOME/.loom/` in machine mode, rather than removing
pid-file tracking altogether. `$HOME/.loom/` is not new state — it is the
**existing** machine-level state home (socket, token pool, `activity.db`,
`daemon.log` already live there); this only adds a few more files to a
directory that was already the machine-level source of truth, and the
pid-file/nohup fallback tier every lifecycle script's own ownership-detection
logic already has (see `loom-daemon-update.sh`'s `DAEMON_MANAGER` resolution)
keeps working unchanged. No existing state (socket, tokens, `activity.db`,
logs) moves. Dropping pid files entirely under launchd remains available as a
future, more invasive follow-up if the pid-file tier ever proves more
confusing than useful in machine mode.

### `restart` verb (Gap 3)

`loom restart` mirrors `start`/`stop` — same collision guard — and prefers the
**supervised in-place** restart: it first tries the daemon's own restart IPC
(`loom-daemon restart`, #4077), which can apply a code update without touching
the loaded launchd job at all. If that is unavailable (not supervised) or
refused (not currently running, or a pre-#4077 binary), it falls back to a plain
stop-then-start via the same checkout-resolved lifecycle-script delegates — on
launchd this does bootout+bootstrap the job, which no longer tears down
in-flight sweeps on a current build (every sweep runs in its own process group,
#5081), though it still cannot apply a plist `EnvironmentVariables` change
without that reload (#4995).

> **It is not a drain (#5119).** A plain `loom restart` does not wait for
> in-flight work, and what happens to that work is **supervisor-specific**: on
> launchd sweep/role-run children reparent to pid 1 and survive, while on
> systemd they sit in the daemon unit's cgroup and the stop job terminates them.
> The wrapper no longer claims otherwise, and the primitive prints the
> supervisor-specific truth. Use `loom-daemon restart --drain` when in-flight
> sweeps must finish first.

### Sweep dispatch on a multi-repo worker host (#4299)

The lifecycle hand-off above (`LOOM_MACHINE_CHECKOUT`) governs `start`/`stop`/
`update`/`restart` — it does **not** apply to `loom-daemon dispatch <issue>` or
the MCP `dispatch_sweep` tool, which target a **repo's working tree**, not the
machine checkout. A worker host provisioned with the machine-level layout
(checkout at `~/.local/share/loom`, one or more product repos registered via
`loom-daemon workspace add`) used to require restarting the daemon with an
explicit `WorkingDirectory=<repo>` override — collapsing the machine-level
daemon back into a single-repo daemon — because `dispatch`/`dispatch_sweep`
resolved an absent `--workspace`/`workspace_root` from the daemon's own cwd
instead of the workspace registry. This is fixed: dispatch now consults
`~/.loom/workspaces.json` for the explicit-param-absent case, so a daemon
started with cwd = the machine checkout and exactly one registered workspace
dispatches into that workspace with no `WorkingDirectory` override needed. See
[`daemon-reference.md`](daemon-reference.md) → `dispatch_sweep` for the full
resolution precedence.

### Testing against a scratch registry (`LOOM_WORKSPACES_PATH`, #4326)

`~/.loom/workspaces.json` is a **machine-level, cross-repo, cross-session**
file — never scope a test or an ad-hoc verification step at it directly. Both
the `loom-daemon workspace add|remove|list|set-priority` CLI and the daemon
itself honor `LOOM_WORKSPACES_PATH` (`loom-daemon/src/workspace_registry.rs`)
as a redirect: when set, every registry read/write for that process goes to
the given file instead of the real one. This is the sanctioned seam for
**any** code that needs to exercise registry behavior — every registry unit
test in `loom-daemon/src/workspace_registry.rs` already uses a tempdir this
way, and it is the correct tool for a builder/auditor session manually
verifying `workspace add`/`status`/priority behavior too:

```bash
LOOM_WORKSPACES_PATH=/tmp/scratch-workspaces.json loom-daemon workspace add /tmp/some-dir --priority 3
LOOM_WORKSPACES_PATH=/tmp/scratch-workspaces.json loom-daemon workspace list
```

Skipping this and calling the real CLI directly leaves stray entries in the
operator's actual registry. Issue #4326 is the incident that motivated this
note: an agent session's ad-hoc registry verification (during a migration/
registry-adjacent sweep) registered `/private/tmp/mig-test` against the real
file, the scratch directory was later deleted, and the dangling entry sat at
explicit dispatch priority `3` — ahead of every real managed repo — for most
of a day, until `loom-daemon workspace remove /private/tmp/mig-test` cleared
it manually. Two structural backstops now exist for the residual case (an
operator or agent forgetting the env var, or a directory going missing after
correct registration): a `PreToolUse` guard hook asks for confirmation before
a real-registry-mutating `workspace` command runs without
`LOOM_WORKSPACES_PATH` in play (`guards.workspaceRegistry`, see
[`guard-hooks.md`](guard-hooks.md) → "Workspace Registry Guard"), and both
`loom-daemon status` and the autonomous work-finder flag/skip a registered
root whose directory no longer exists on disk (warn-and-skip, never
auto-remove — a root can be transiently absent, e.g. an unmounted volume).

### Supervision (reboot/crash) — macOS via launchd, Linux via systemd `--user`

Reboot/crash supervision itself (as opposed to the workdir/pid-file relocation
above) is implemented for macOS via launchd — `RunAtLoad` (#3972),
`KeepAlive:{SuccessfulExit:true}` restart-only relaunch (#4054), and a
`StartInterval` autonomy-loss watchdog (#4011), all resolved through the
`gui/<uid>` ↦ `user/<uid>` domain fallback (#4130).

On a **systemd Linux host** the same reboot survival + supervised-restart contract
is provided by a `systemd --user` service (#4268, sub-issue B of #4260): `loom
start` installs `~/.config/systemd/user/loom-daemon.service` and `systemctl --user
enable --now`s it (`Restart=on-success` == the launchd restart-only relaunch;
`WantedBy=default.target` == `RunAtLoad`), and `loom stop` runs `systemctl --user
disable --now` so a reboot does not resurrect it. **Reboot survival on a
headless / SSH-only host requires lingering** — run `loginctl enable-linger
"$USER"` once. A non-systemd host (or `--no-systemd` / `LOOM_DAEMON_SYSTEMD=0`)
falls back to the plain `nohup` path. See [`daemon-reference.md`](daemon-reference.md)
→ Operability → "systemd user unit (Linux)" for the full writeup. Crash relaunch
(as opposed to restart-on-success) and a systemd-side autonomy-loss watchdog
remain follow-ups (sub-issue D of #4260), mirroring the still-launchd-only #4011
watchdog.

## User-scope skills + agents (Epic #3835 Phase 4, #4261)

The `/loom:*` slash-command skills and the `loom-*` subagents also resolve from
the machine-level checkout, provisioned by
`scripts/install/provision-skills.sh` (sibling of `provision-dispatcher.sh`)
next to the dispatcher install:

```
~/.claude/commands/loom      -> <checkout>/defaults/.claude/commands/loom   (whole-directory symlink)
~/.claude/agents/loom-<role>.md -> <checkout>/defaults/.claude/agents/loom-<role>.md  (per-file symlinks)
```

Why the two shapes differ:

- **Commands are one whole-directory symlink.** `.claude/commands/loom/` is a
  Loom-owned namespace, so a single link is the minimal surface, keeps
  `.claude/commands/` itself a real directory (a co-installed tool can still
  write sibling namespaces into it), and preserves the relative cross-references
  between skill files (e.g. one skill `@`-including `probe-protocol.md`).
- **Agents are per-file symlinks.** `~/.claude/agents/` is **shared** with the
  operator's own agents, so it cannot be wholesale-symlinked; each
  `loom-<role>.md` is linked individually and non-Loom agents are untouched.

Both link **through the checkout path** (`<checkout>/defaults/...`, default
checkout `~/.local/share/loom`), never the installer's transient source root.
Because `~/.local/share/loom` is itself a symlink to the source checkout, the
links stay valid even if the machine checkout is later re-provisioned, and
**`loom update` (refreshing the checkout) updates the skills/agents seen by
every repo at once** — they cannot drift per-repo.

Provisioning is additive and best-effort (never fatal). It:

- **Never clobbers a real destination.** An operator's hand-tuned
  `~/.claude/agents/loom-judge.md`, or a real `~/.claude/commands/loom/`
  directory, is left as-is with a warning.
- **Repoints a stale symlink** (e.g. one left by an older checkout location) to
  the current checkout.
- **Prunes a dangling `loom-*` agent link** whose target no longer exists in the
  checkout (a renamed/removed role) — but only when the broken link points into
  a checkout's `defaults/.claude/agents/` tree, never an operator's own link.

### Transition precedence (not a bug)

Until Phase 6 (#4254) strips the committed per-repo copies, an already-installed
consumer repo keeps its `.claude/commands/loom/*` files, and Claude Code
resolves a **project-level** definition over a **user-level** one when names
collide. Those repos therefore keep resolving their (possibly stale) copies
until migration; this provisioning only changes what **fresh installs and
post-migration repos** resolve. The coexistence is expected and does not error —
provisioning does not try to force user scope to win.

> **Scope boundary (Phase 4 vs Phase 6).** This phase adds the user-scope
> resolution *mechanism* (the replacement Phase 6 depends on). Ceasing the
> per-repo copy on a fresh install is coupled to `loom-daemon init` (Rust),
> which materializes `.claude/commands/loom/` + `.claude/agents/` and whose own
> post-init validation (and the installer's `EXPECTED_FILES` check) currently
> requires those trees present. That cutover lands with Phase 6's
> `git rm --cached` migration, not here.

## User-scope guard hooks (Epic #3835 Phase 5, #4262)

The `PreToolUse` / `UserPromptSubmit` / `Stop` guard **hooks** also resolve from
the machine-level checkout, provisioned by `scripts/install/provision-hooks.sh`
(sibling of `provision-skills.sh`). Rather than a symlink, this performs a jq-based
**idempotent merge** into the operator's user-scope `~/.claude/settings.json`:
create-if-missing, back up before the first write, dedupe by the machine-level
marker substring `defaults/hooks/<name>` (survives Claude Code requoting — the
#4200 lesson), preserve every non-Loom entry, and soft-fail (no write) on invalid
existing JSON.

Each wired command is a **fail-open, self-gating** wrapper (full behavior in
`defaults/docs/guard-hooks.md` → "Machine-Level Execution"): it no-ops outside a
Loom workspace, defers to a still-present per-repo `.loom/hooks/<name>` copy
(transition precedence — the project copy wins until Phase 6 / #4254 strips it, so
guards never double-fire), and otherwise exec's the machine-checkout hook with the
resolved repo root passed via `LOOM_PROJECT_ROOT`. `$HOME` / `$LOOM_HOME` expand
per-user at hook-invocation time, so one wired command is correct for every
operator. Daemon-spawned workers inherit the wiring via the `~/.claude/settings.json`
copy each worker's isolated `CLAUDE_CONFIG_DIR` receives.

`loom-daemon init`'s `.claude/settings.json` merge (`scaffolding.rs`) recognizes
this machine-level command form (`MACHINE_HOOK_MARKER`) alongside the legacy /
project-relative prefixes, so a reinstall never duplicates and an uninstall never
orphans an entry that lands in a project-level settings file.

> **Scope boundary (Phase 5 vs Phase 6).** This phase ships the machine-level hook
> *execution* + user-scope wiring, and stops **copying** `defaults/hooks/*.sh` into
> a fresh install's `.loom/hooks/`. It does **not** remove existing per-repo copies
> or their project-level settings entries — that is Phase 6's `git rm --cached`
> migration, which this replacement unblocks.

## `loom migrate`: historical consumer install → daemon model (Epic #3835 Phase 6, #4254)

`loom migrate` takes a repo carrying a **historical file-copy install** (a full
committed copy of the Loom implementation under `.loom/`,
`.claude/commands/loom/`, `.claude/agents/loom-*.md` plus a legacy
`.loom/config.json` — the pre-daemon layout every consumer repo on ≤ 0.12 holds)
to the machine-level daemon model in **one idempotent pass**. It is the final
phase of Epic #3835: Phases 1–5 stood up the machine checkout, user-scope skills /
agents / hooks / mcp, and the workspace registry; this phase retires the per-repo
file copies that would otherwise shadow them and drift stale.

Run it from inside the repo:

```bash
loom migrate --dry-run          # full file-by-file plan; makes no changes
loom migrate                    # apply; prints a per-file report, stages the result
loom migrate --priority 20      # workspace-registry priority (default 3)
loom migrate --force            # proceed despite an uncommitted working tree
```

The dispatcher resolves the current repo root and delegates to
`scripts/install/migrate-consumer.sh` in the machine checkout, exporting
`LOOM_MACHINE_CHECKOUT` so the migration reads the current `defaults/` ownership
boundary no matter where it is invoked. What one pass does:

1. **Detect** the historical install from `.loom/install-metadata.json` + its
   `installed_files` manifest. No metadata/manifest → a clean refusal, zero
   changes. An uncommitted working tree → refuse unless `--force` (so the
   migration's staged `git rm --cached` never entangles unrelated edits).
2. **Extract project config** from the legacy `.loom/config.json` into the
   **tracked** `.loom-project/project.json` (the Phase 2 resolver's project tier)
   — **excluding `sweep.modelAliases`**. That key's Rust/Python resolvers diverge;
   migrating it would freeze the divergence into every consumer repo, so it is
   left in the (lower-precedence, on-disk) legacy tier and reported as excluded.
   **Host-local keys** (`worktree.root` — a per-host scratch-disk path) are routed
   to the **gitignored** `.loom-local/local.json` (the resolver's `LOCAL_CONFIG_REL`
   tier), *not* the tracked, shared `project.json`: since this same pass
   `git rm --cached`s the legacy config, `project.json` becomes the highest tier
   every fresh clone / CI run picks up, so a stray `worktree.root` there would
   silently propagate one operator's filesystem layout to the whole team. An
   existing `project.json` is left untouched (idempotency); an existing
   `local.json` override is preserved (only a missing `worktree.root` is filled in).
3. **Untrack the committed implementation** per the manifest — `git rm --cached`
   (files stay on disk, just leave the index) + a gitignore block single-sourced
   with `install-loom.sh --local`. Only the machine-served namespaces (`/.loom/`,
   `/.claude/commands/loom/`, `/.claude/agents/loom-*.md`) are untracked; genuinely
   repo-level manifest entries (`.github/*`, `.codex/*`, `.gitignore`,
   `package.json`) stay **tracked**. Nothing outside the manifest is ever touched
   (the #3450 ownership rule).
4. **Remove deprecated artifacts** — the `loom-iteration.md` / `loom-parent.md`
   two-tier-daemon tombstones (removed as a subsystem in v0.10.0, #3372) and any
   manifest entry under a Loom namespace no longer shipped by current `defaults/`
   (shepherd-era scripts) are deleted from disk **and** index.
5. **Preserve**: `.loom/resync-ignore` pins stay tracked and are reported;
   locally-modified files are surfaced (the working copy is never deleted — `git
   rm --cached` only touches the index); runtime state (`.loom/logs/`,
   `.loom/sweep-checkpoint/`, `.loom/tokens/`) and the `loom.sh` / `.loom/bin/loom`
   shims are never touched, so `./loom.sh` and the per-repo pool manager keep
   working.
6. **Repair the MCP wiring** (#4386). A historical repo can carry a repo-scoped
   `.mcp.json` whose `loom` server entry points into a long-dead worktree bundle
   (e.g. `.loom/worktrees/issue-N/mcp-loom/dist/index.js`). Because Claude Code MCP
   precedence is **local > project > user**, that repo-scoped entry silently
   *shadows* the machine-level user-scope server — and when its path is a dead
   worktree it kills every daemon-dispatched child at the claude-wrapper MCP
   pre-flight (a fleet-wide spawn outage with no surfaced error). Migration strips
   any repo-scoped `loom` entry from `.mcp.json` (deleting the file if `loom` was
   its only server; other servers such as `safehouse` are kept — matching
   `setup-mcp.sh`'s #4230 migration), then **verifies the user-scope `loom`
   registration** exists and points at the machine checkout's
   `mcp-loom/dist/index.js`, `claude mcp add --scope user`-ing it if absent or
   mis-pointed (skipped with a clear note when the `claude` CLI is not on PATH).
7. **Register** the repo via `loom-daemon workspace add … --priority N` (skipped
   with a clear note when `loom-daemon` is not on PATH), **re-stamp**
   `.loom/install-metadata.json` (`loom_version`, `loom_commit`,
   `migrated_to_machine_model`, `install_model`), and **refresh** the CLAUDE.md
   `<!-- BEGIN/END LOOM ORCHESTRATION -->` marker section to the daemon-model
   surface.

The migration **stages** its result and leaves the commit to the operator —
review with `git status`, then commit. A second run is a clean no-op (an existing
tracked `.loom-project/project.json` short-circuits extraction and the manifest
paths are already untracked). Fixture-based coverage:
`scripts/test-migrate-consumer.sh`.

> **Skills stay installed, not stripped.** After migration the role skills
> (`/builder`, `/judge`, `/curator`, `/doctor`, `/loom:sweep`, `/loom`, …) still
> resolve in consumer sessions — now from the **user-scope** machine checkout
> (Phase 4), so they track the installed daemon version instead of the stale
> per-repo copies this pass removes. Migration replaces the copies with the
> always-current shared skills; it does not remove manual capability. It **does**
> require user-scope provisioning to have run on the host first (Phases 4/5) —
> otherwise a migrated repo would lose its now-untracked skills with no
> replacement.

## Uninstall semantics

A per-repo `uninstall-loom.sh` removes only the per-repo `./.loom/bin/loom` pool
manager. The machine-level `~/.local/bin/loom` dispatcher and the
`~/.local/share/loom` checkout link are **not** removed by a per-repo uninstall —
same semantics as the shared `~/.local/bin/loom-daemon` binary, which outlives any
single repo's uninstall.

The user-scope skill + agent links follow the **same** shared-resource rule:
they are one set resolved by every repo on the machine, so a per-repo uninstall
does not remove them (that would break `/loom:*` for every other consumer repo).
A machine-level teardown removes them via `deprovision_loom_skills` in
`scripts/install/provision-skills.sh`, which deletes a link only when it points
into the machine checkout and never touches an operator's real files.

The user-scope **hook** entries follow the identical rule: they are one shared set
resolved by every repo, so a per-repo uninstall does not strip them (that would
disable the destructive-command / worktree guards for every other consumer repo). A
machine-level teardown removes them via `deprovision_loom_hooks` in
`scripts/install/provision-hooks.sh`, which removes only entries carrying the
`/defaults/hooks/` marker and never touches an operator's own hooks. The per-repo
**project-level** `.claude/settings.json` Loom hook entries, by contrast, *are*
stripped by `uninstall-loom.sh`'s jq smart-removal on that file.
