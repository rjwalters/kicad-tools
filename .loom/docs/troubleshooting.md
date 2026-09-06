# Loom Troubleshooting Guide

## Common Issues

### Hooks not firing (`guard-destructive.sh` not blocking commands)

**Symptom**: Commands that should be blocked or confirmed by `guard-destructive.sh` (e.g., `git reset --hard`, `gh issue close`) are executing without any prompt or denial.

**Root cause**: Claude Code's `--permission-mode bypassPermissions` flag skips ALL PreToolUse hooks entirely. If Claude Code is invoked with this flag, hooks never run — not even safety hooks like `guard-destructive.sh`.

**How to diagnose**:
```bash
# Check if you have a shell alias that sets bypassPermissions
alias claude 2>/dev/null || echo "no alias"

# Check if Loom scripts are using the correct flag
grep -r 'permission-mode' .loom/scripts/ .loom/roles/ 2>/dev/null
```

**The two flags behave differently**:

| Flag | Hooks fire? | Use case |
|------|-------------|----------|
| `--dangerously-skip-permissions` | ✅ YES | Loom automation (agents use this) |
| `--permission-mode bypassPermissions` | ❌ NO | Fully bypasses all permission checks AND hooks |

**Fix**: If you have a shell alias using `--permission-mode bypassPermissions`, change it to use `--dangerously-skip-permissions` instead:

```bash
# WRONG - hooks silently disabled:
alias claude="claude --permission-mode bypassPermissions"

# CORRECT - hooks still fire:
alias claude="claude --dangerously-skip-permissions"
```

Note: `--dangerously-skip-permissions` still skips interactive permission prompts (so agents can run non-interactively), but hooks are executed. This is the intended mode for Loom agents.

**Verify the fix**: After updating your alias, restart your shell and confirm hooks fire by checking the hook error log:
```bash
# Hook invocations log errors here:
cat .loom/logs/hook-errors.log
```

If the log is absent or empty and hooks aren't blocking, confirm Claude Code is invoked with `--dangerously-skip-permissions` (not `bypassPermissions`).

### `worktree.sh N` skips a stale post-squash-merge remote branch (#5657)

`worktree.sh N` prefers reusing `refs/remotes/origin/feature/issue-N` over
branching fresh from the base ref when no local copy exists (#4823, so an
in-flight Doctor/Builder cycle's real PR history isn't silently discarded).
But if that remote branch's current tip is already the head of an
already-**merged** PR — e.g. the target repo has "auto-delete head branches"
disabled, or any other path that leaves a merged branch's ref on `origin` —
reusing it would build the new worktree on top of already-merged, now
foreign-to-`main` history, producing a `CONFLICTING` PR with zero CI runs.
This matters most with the partial-increment (#3667/#3599) slice convention,
where the *same* branch name `feature/issue-N` is deliberately reused across
an issue's slices, so a squash-merged prior slice's branch can still be
sitting on `origin` when the next slice's worktree is created.

`worktree.sh` now checks the remote branch's tip against the forge (reusing
the same `_worktree_merged_pr_head_sha` helper already used by the worktree
**removal** path, #4889) before reusing it: if the tip matches an
already-merged PR's head, it creates a fresh branch from the base ref instead
and prints which PR made the old branch stale. If the forge lookup is
unavailable (network/auth failure), it fails open to the pre-existing reuse
behavior — a forge outage never blocks worktree creation. The #4823 in-flight
case (remote branch exists, not yet merged, possibly diverged from base) is
unaffected and still reused exactly as before.

### `git push --force-with-lease` prints a rejection for a ref update that landed (#6695)

On a repository using Git LFS, `git push --force-with-lease=<branch>:<old-sha>
origin <branch>` can print a rejection —

```
! [rejected]        <branch> -> <branch> (stale info)
error: failed to push some refs to '...'
remote rejected ... is at <new-sha> but expected <old-sha>
```

— even though the ref update actually **landed** (confirmed by `git
ls-remote` and the `origin/<branch>` reflog showing an `update by push`
entry). Suspected cause: the **LFS pre-push hook** races the lease
re-check — the hook uploads LFS objects and the ref update proceeds on the
remote, while the printed rejection reflects a lease comparison read at a
different (already-stale) moment. Both known occurrences were on branches
with pending LFS objects to upload; a same-session push with nothing to
upload did not exhibit it.

**Do not treat this message as an automatic retry/re-rebase signal.** An
agent that trusts the printed rejection alone may retry the push, re-rebase,
or report failure against a ref that already moved — all of which turn a
clean state into a confusing one. Before reacting to a `force-with-lease`
rejection, check the *live* remote ref yourself:

```bash
git ls-remote origin "refs/heads/<branch>"   # compare against your local tip
```

If they already match, the push landed — do nothing further. Loom's own
stacked-PR automation (`reconcile-stack.sh`, `rebase-stacked-children.sh`)
performs exactly this check via the shared
`defaults/scripts/lib/push-lease-verify.sh` helper
(`push_landed_despite_rejection`) whenever a `--force-with-lease` push
reports failure, and logs the condition with a greppable
`PUSH-LEASE-RACE-DETECTED` marker so it is never silently folded into an
ordinary failure. A genuine rejection (the ref really did not move) is still
reported and handled as a failure — the check only reclassifies a reported
rejection whose ref update is confirmed to have landed.

### Cleaning Up Stale Worktrees and Branches

Use the `loom-clean` command to restore your repository to a clean state:

```bash
# Interactive mode - prompts for confirmation (default)
loom-clean

# Preview mode - shows what would be cleaned without making changes
loom-clean --dry-run

# Non-interactive mode - auto-confirms all prompts (for CI/automation)
loom-clean --force

# Deep clean - also removes build artifacts (target/, node_modules/) IN FULL,
# service binaries built there included. --safe does not narrow this (#6127);
# a directory backing a *running* program is skipped whole, a stopped one is not.
loom-clean --deep

# Combine flags
loom-clean --deep --force  # Non-interactive deep clean
loom-clean --deep --dry-run  # Preview deep clean
```

`loom-clean` is a thin shim for `loom-daemon clean` and needs a `loom-daemon`
binary built at or after commit `dba33666` (PR #4301) — see [fail on a stale
binary](#loom-clean--cleanupsh--loom-recover-orphans-fail-on-a-stale-binary-4384)
if it errors out instead of running.

**What loom-clean does**:
- Removes worktrees for closed GitHub issues (prompts per worktree in interactive mode)
- Deletes local feature branches for closed issues
- Cleans up Loom tmux sessions
- (Optional with `--deep`) Removes `target/` and `node_modules/` directories —
  in full, including service binaries built there; see
  [Never launch a service from a build-output path](#never-launch-a-service-from-a-build-output-path-6127)

**IMPORTANT**: For **CI pipelines and automation**, always use `--force` flag to prevent hanging on prompts:
```bash
loom-clean --force  # Non-interactive, safe for automation
```

**What `--safe` actually narrows (#4890)**: `--safe` is documented as
"merged-PR-only mode", but that promise only has meaning for artifacts that
*are* an artifact of a merged PR — worktrees (gated on issue-closed +
PR-merged + grace period) and branches. A tmux session has no PR association
at all, so `loom-clean --safe` skips tmux cleanup **entirely** rather than
silently killing a live session (a Manual-Orchestration-Mode terminal on
Loom's isolated `-L loom` tmux socket does not show up in a plain `tmux ls`,
so an operator has no other way to notice). Use `loom-clean --tmux-only`
(optionally with `--force`) outside `--safe` to clean tmux sessions
explicitly. Even then, a session with an attached client (someone is actively
looking at it) is preserved unless `--force` is passed.

**`--safe` does NOT narrow `--deep` (#6127)**: the same reasoning applies to
build artifacts — `target/` and `node_modules/` have no merged-PR concept
either — but with the opposite consequence. Rather than being *skipped* like
tmux, they are removed **in full under `--safe` exactly as under a bare
`--deep`**. `loom-clean --deep --safe` is not a gentler deep clean; it is a
deep clean with gentler worktree/branch handling. This was previously
inferable only from #4890's discussion of the two classes it does gate.

### Never launch a service from a build-output path (#6127)

`--deep` deletes `target/` wholesale, and a **service binary built there is
just another build artifact** to it. A launchd/systemd unit whose `program` is
`<repo>/target/release/<bin>` therefore gets its backing file unlinked by a
routine clean. Nothing fails at delete time — the kernel keeps the running
process alive on the unlinked inode — so the unit stays `active (running)`,
every liveness check passes, and the outage only fires at the **next restart**,
where the supervisor cannot exec a missing path (launchd: `exit code 78:
EX_CONFIG`). The confirmed repro ran three days in that state; `readlink
/proc/<pid>/exe` reported `… (deleted)`, which is the fastest way to check a
suspect host:

```bash
pid=$(pgrep -x <service>); readlink "/proc/$pid/exe"   # "… (deleted)" ⇒ already armed
```

As of #6127 `clean --deep` **detects this and refuses**: before removing a
build-artifact directory it scans the process table (Linux `/proc/<pid>/exe`,
macOS/BSD `ps -o comm=`) and, if any live process is executing a binary inside
it, keeps the whole directory and prints `SKIPPED target/ is backing N live
process(es) [pid … → …]`. The scheduled pass logs the same line at `WARN`. This
is an ungated floor — there is no `--force` override and no config toggle,
because an escape hatch a scheduled job could set would reinstate the exact
silent-outage bug. The disk is not lost, only deferred: stop the service and
re-run.

**Two limits worth knowing**, both of which mean the operator-side rule still
stands:

- A service that is **stopped** when the clean runs is invisible to a process
  scan. Its `program` is deleted and the next start fails identically.
- Detection covers processes whose executable path the running user can read
  (`/proc/<pid>/exe` is unreadable for other users' processes).

So: build wherever you like, but **install** what you run — copy the binary to
`~/.local/bin` (or a package path) and point the unit at that. `loom-daemon`
itself was only ever immune to this by that accident of install location.

### `.loom/logs/` retention (#6655)

`.loom/logs/` has no bound of its own — every sweep writes its own per-issue
log (`sweep-issue-<N>.log` from `claude-wrapper.sh`,
`loom-daemon-sweep-issue-<N>-<ts>.log` from `loom-daemon`) directly into that
directory, and nothing removed them before this. On a saturated fleet host
this grows unbounded (one incident: 310 MB across 4,114 `.log` files, the
oldest six months stale). `archive-logs.sh`'s own `--retention-days` (default
7, invoked via `loom-daemon cleanup logs`) does **not** cover this — it only
prunes its own dated `.loom/logs/YYYY-MM-DD/` archive subdirectories (task
output archives + daemon-state snapshots), a different mechanism entirely.

**`loom-clean` (and the scheduled `com.rjwalters.loom-fleet-clean` launchd
job, which already runs it) now also prunes stale per-issue logs** as part of
its normal "Cleaning Stale Logs" phase — no separate flag needed, and
`--dry-run` lists what it would remove without deleting anything:

```bash
loom-clean --dry-run   # lists stale logs it would remove, alongside worktrees/branches
loom-clean --force     # actually removes them
```

Only files directly under `.loom/logs/` whose name embeds an issue number
(the `sweep-issue-<N>.log` / `loom-daemon-sweep-issue-<N>-<ts>.log` /
`issue-<N>-<role>.log` conventions) are ever candidates — singleton
accumulator logs with no issue number (`role-<name>.log`,
`guard-decisions.log`, `hook-errors.log`, `daemon-start.log`,
`main-quarantine.log`, `worktree-removals.log`, …) and anything under a
subdirectory (`archive-logs.sh`'s own dated archives, `skill-router-seen/`,
…) are never touched by this pass. A candidate log is removed only once ALL
of:

1. Its mtime is older than the retention window (below).
2. Its issue has no live sweep right now (no `.loom/locks/issue-<N>/` claim
   lock, no `.loom/spawn-loop-state.json` entry).
3. Its issue has no `.loom/sweep-checkpoint/issue-<N>.json` — a resumable
   sweep (even one not currently running) keeps its log regardless of age.

**Retention window** — precedence **env > config > default (30 days)**:

```jsonc
// .loom/config.json
{ "logs": { "retentionDays": 14 } }
```

```bash
LOOM_LOGS_RETENTION_DAYS=14 loom-clean --dry-run
```

A non-positive or unparseable value at either tier is treated as absent (falls
through to the next tier), never as "disable retention" — a config typo can
never silently turn this pass into a no-op.

**Backlog of pre-existing `[gone]` local branches (#4100)**: `merge-pr.sh` deletes
the local feature branch for every PR it merges as of #4100, but repos that ran
Loom before that fix accumulated one orphaned local branch per merged issue —
each pointing at a remote ref the merge already deleted server-side, so `git
branch -vv` shows them as `[gone]`. Reap the existing backlog with:

```bash
git fetch --prune   # drop stale remote-tracking refs first
loom-clean --force  # clean_branches() deletes local branches whose remote is gone
```

`loom-clean`'s `clean_branches()` already handles this in two passes: a
pattern-agnostic pass that deletes any local branch (other than the
default/current/other-worktree-checked-out ones) whose `origin/<branch>` no
longer exists, plus an issue-state pass that probes `feature/issue-*` branches
still tracking a remote against the forge and deletes them once the issue is
closed. No separate one-off backlog tool is needed.

**Manual cleanup** (if needed, but use with caution):

**WARNING**: Running `git worktree remove` while your shell is in the worktree directory will corrupt your shell state. Always ensure you've navigated out of the worktree first, or use `loom-clean` which handles this safely.

```bash
# First, ensure you're NOT in the worktree you're removing
cd /path/to/main/repo

# List worktrees
git worktree list

# Remove specific stale worktree (only after navigating out!)
git worktree remove .loom/worktrees/issue-42 --force

# Prune orphaned worktrees
git worktree prune
```

### Redirected cargo target dirs are reclaimed with their worktree (#7239)

**Symptom**: an external volume fills with directories like
`/Volumes/build/cargo-target/issue-<N>` whose worktrees were removed weeks ago.
One multi-agent host accumulated tens of them and hundreds of GB before the
volume started pressuring other workloads.

**Cause**: Cargo's output is only `<worktree>/target` by default.
`CARGO_TARGET_DIR`, or `build.target-dir` in any `config.toml` on Cargo's
lookup path (commonly `~/.cargo/config.toml` on a host tuned by
`/repo:host-optimize`), redirects it **outside** the worktree — where no
removal path ever looked. Removing the worktree therefore left the build
output behind forever.

**Now**: every worktree-removal path resolves the *actual* target dir before
removing the worktree (`cargo metadata`, so the value it reads honors the whole
config hierarchy) and reclaims it afterwards — but only when the redirect is
attributable to that one worktree, which means a `.cargo/config.toml` inside it
(see the never-delete list and the limitation below):

- `worktree.sh remove <N>` — bash side, via
  `defaults/scripts/lib/cargo-target-dir.sh`.
- `merge-pr.sh`'s post-merge worktree cleanup — same bash library. This is the
  removal path most worktrees actually take, so it is where the leak was
  largest.
- `loom-daemon clean` and the daemon's periodic worktree reaper — Rust side,
  via `loom-daemon/src/worktree_ops/cargo_target.rs` (both `issue-<N>` and
  `pr-<N>` worktrees).

Preview what a removal would reclaim, with sizes, without touching anything:

```bash
./.loom/scripts/worktree.sh remove 42 --dry-run    # or --dry-run --json
loom-daemon clean --dry-run                        # every candidate worktree
```

**What it will never delete**, each reported with the reason instead:

- A dir that is only ever named by a **machine-global redirect source**. Two
  of those exist, and neither can be evidence that a directory belongs to the
  one worktree being removed, because both resolve identically for *every* path
  on the host:
  - the **remover's own ambient `CARGO_TARGET_DIR`**, read from the environment
    of whatever process is doing the removal; and
  - `build.target-dir` in **`$CARGO_HOME/config.toml`, or in any ancestor
    `.cargo/config.toml` above the worktree** — including the machine-wide one
    `/repo:host-optimize` writes.

  Only a redirect derived from the worktree **itself** — `build.target-dir` in a
  `.cargo/config.toml` *inside* the worktree — authorizes a delete. On a host
  whose single shared build cache comes from either global source, worktree
  removal simply reports and leaves it alone.
- A dir another **live** worktree also resolves to — the `host-optimize`
  convention is a *single shared* `target-dir` for the whole machine, and
  deleting that on one worktree's removal would destroy a sibling's cache
  mid-build. Containment counts in both directions, and if the sharing question
  cannot be answered it refuses rather than assuming nobody else uses it. That
  covers both ways it can go unanswered: `git worktree list` failing, and a
  live worktree whose configured redirect `cargo metadata` could not read (a
  mid-edit `Cargo.toml`, a conflicted merge). A sibling that silently degraded
  to `<sibling>/target` would otherwise look exactly like one that builds
  somewhere else.
- A dir a **running process** is using (same evidence-based gate as
  "Never launch a service from a build-output path" above). The reclaim is
  deferred, not lost — stop the process and remove again.
- The repository itself or any ancestor of it, the primary checkout's own
  `target/` (that belongs to the pressure-gated deep-clean pass), `$HOME`, or
  any suspiciously shallow path.

An un-redirected `target/` inside the worktree is reported as nothing at all:
it disappears with the worktree for free, as it always did.

**Limitation — the redirect must be attributable to the worktree, and
resolvable at removal time.** The reclaim asks Cargo where this worktree's
output *would* go right now (`CARGO_TARGET_DIR` → `cargo metadata` →
`<worktree>/target`); it does not guess at a naming template and never deletes
a path by pattern match. Two shapes are therefore reported rather than
reclaimed:

- A *per-worktree* `CARGO_TARGET_DIR` exported only inside the agent/build
  environment (say `…/cargo-target-<issue>`), with the worktree then removed
  from a shell where that variable is unset. Resolution lands somewhere else
  and the real directory is left alone — correctly, since nothing at removal
  time can prove it belonged to that worktree. Re-exporting the variable for
  the removal does **not** help: an env var read from the remover's own
  environment is machine-global and is refused outright (see the never-delete
  list above). Put the redirect where it belongs to the tree instead —
  `build.target-dir` in a `.cargo/config.toml` **inside the worktree**.
- A redirect that lives only in `~/.cargo/config.toml` (what
  `/repo:host-optimize` writes) or in an ancestor `.cargo/config.toml`. Every
  worktree on the host resolves to that one directory, so no single worktree's
  removal may delete it; the reclaim treats such a tree as unredirected and
  does nothing. To get per-worktree reclaim, give each worktree its own
  `.cargo/config.toml` with a `target-dir` of its own (for example
  `…/cargo-target/issue-<N>`) — that is the shape this feature reclaims.
- A worktree whose Cargo manifest is not at its **root** (a nested
  `backend/Cargo.toml` layout). The pre-check treats a root without a
  `Cargo.toml` as "cargo never built here", so nothing is resolved or
  reclaimed. That is a missed reclaim, not data loss — the safe failure
  direction — but such a directory must be removed by hand.

Directories orphaned *before* this landed have no worktree left to resolve
from, so they must be removed by hand — check them against `git worktree list`
first, and stop anything still building into them.

### A worktree vanished mid-session — who removed it? (#5950)

**Symptom**: a Builder's worktree and/or its `feature/issue-N` branch disappears
while work is still in progress, and the loss shows up as "my branch is gone" or
"my commits are gone". This is the shape reported in #5950 (lost during issue
#5919's session), where a `loom-daemon clean` transcript in the same shell had
explicitly printed `Issue #5919 is OPEN - preserving`.

**First move — read the removal ledger** (#5950). Every Loom-owned removal path
appends one JSON line to `.loom/logs/worktree-removals.log`:

```bash
# Everything that removed a worktree for issue 5919
grep 'issue-5919' .loom/logs/worktree-removals.log

# Everything removed in a time window, most recent last
jq -r 'select(.ts > "2026-08-10T20:00:00") | "\(.ts) \(.mechanism) \(.reason) \(.worktree)"' \
  .loom/logs/worktree-removals.log
```

`mechanism` is one of `clean`, `clean --aggressive`, `worktree_reaper`,
`terminal_destroy`, `loom-recover-orphans`, `merge-pr.sh`, `worktree.sh remove`,
`agent-destroy.sh` — all eight of Loom's removal paths; `reason` is the exact
decision that authorized it (e.g. `force_override_unreachable`, `pr_merged`).
**An absent entry is evidence too**: no Loom code path removed it, so look at
host-level/manual action — a bare `rm -rf`, a hand-run `git worktree remove`, a
`git worktree prune` against a directory that was moved, or another checkout of
the same repo on the same host.

Nothing rotates this file (`archive-logs.sh` prunes `.loom/logs/archive/`, not
`.loom/logs/*.log`) — deliberately, since its value is being readable long after
an incident. One ~150-byte line per removal keeps that cheap.

**Second move — the daemon log**. The periodic reaper logs one `info` line per
pass naming both what it removed and what it preserved:

```bash
grep 'worktree_reaper:' ~/.loom/daemon.log | tail -40
# → worktree_reaper: /path/to/repo scanned=14 removed=[] preserved=[5919, 5923, …]
```

Before #5950 the reaper's preservation decisions were `log::debug!` while the
daemon initializes its logger at `info`, so a pass that removed nothing logged
nothing at all and could be neither blamed nor cleared after the fact. The
per-issue *reasons* are still `debug`; run the daemon with `RUST_LOG=debug` to
see them.

**Why the "preserving" line did not protect it.** Loom has more than one
worktree-removal decision surface, and they do not share one gate:

| Path | Consults issue-open state? |
|------|---------------------------|
| `loom-daemon clean` (interactive) | Yes — anything not `CLOSED` is preserved, and prints `Issue #N is <state> - preserving` |
| `worktree_reaper` (daemon, periodic) | Yes — same `classify_worktree` gate as above |
| `loom-daemon clean --aggressive` | **Only since #5950** — see below |
| `loom-recover-orphans` | No — but it removes only a worktree that is 0 commits ahead of `origin/main` with build-artifact-only dirt, so there is nothing to lose |
| `merge-pr.sh` / `worktree.sh remove` / `agent-destroy.sh` / terminal-destroy | No — these are explicitly requested removals |

`clean --aggressive` is a separate decision tree (`worktree_ops/aggressive.rs`)
that reaches its own removal decisions from open-PR + uncommitted-changes +
reachability-from-`origin/main`. Until #5950 it never consulted issue state at
all, so a live Builder session's worktree on an **open** issue was removable by
it — including under plain `--force`, with unpushed local commits and no PR
opened yet. Two things that look like they should have covered that did not: its
`active_shepherd` gate only protects issues holding a `.loom/locks/issue-<N>/`
claim-lock, which **only daemon-dispatched sweeps take** (a manually run
`/loom:sweep` has none), and aggressive mode deliberately overrides
`.loom-in-use` markers and the process-table guard.

Note also that **`-y` is a visible alias of `--force`**, not a separate
"auto-confirm" flag (`loom-daemon clean`'s `#[arg(short = 'f', long,
visible_alias = "yes", visible_short_alias = 'y')] force`). Scripting the
aggressive pass unattended — the obvious `clean --aggressive -y` — therefore
runs it *forcing*, which is the mode that reaches
`Force-remove (HEAD not on origin/main — would lose work)`. Before #5950 that
removal consulted no issue state whatsoever.

**Current behavior (#5950)**: `clean --aggressive` keeps a worktree whose issue
is not `CLOSED` (`UNKNOWN` — a failed forge probe — counts as not closed, fail
closed) **unless the removal cannot lose anything**: the working tree is clean
*and* the work is landed (HEAD reachable from `origin/main`, or a merged PR).
That carve-out is deliberate — partial-increment slices (`Part of #N`) merge
while the family issue stays open indefinitely, and those worktrees must stay
reclaimable. It reports as `Skip (issue is not CLOSED — a Builder may be
mid-session)`. Worktrees with no `issue-N` branch (detached, `pr-NNNN`,
user-provisioned paths) have no issue state to consult and are unaffected.

**What the #5919 post-mortem could and could not establish.** The ledger exists
because this incident was *not* attributable from the evidence that existed at
the time. Against `~/.loom/daemon.log` (+ rotations `.1`–`.5`, covering
2026-08-01 → 2026-08-11) for the incident window — issue #5919 filed
`2026-08-10T21:01:18Z`, PR #5942 opened `23:31:21Z`:

- **Ruled out — the periodic reaper.** The string `5919` does not appear in the
  daemon log at all. Reaper *removals* are `info`-level and name the issue
  (`worktree_reaper: <repo> scanned=14 removed=1 … removed=[5916]`), so a
  removal would have been logged; none was.
- **Ruled out — the daemon's terminal-destroy path.** `Removing worktree at …`
  (also `info`) appears zero times in the whole log.
- **Ruled out — the scheduled fleet clean.** The `com.rjwalters.loom-fleet-clean`
  launchd job runs `loom-daemon clean --workspace <w> --deep --safe -y` and never
  `--aggressive`; its first run was `2026-08-11T01:02:44Z`, after the window.
- **Not exculpated — `clean --aggressive` and manual/host-level removal.** Both
  were unfalsifiable: aggressive mode is an interactive CLI that printed to a
  terminal and wrote no persistent artifact anywhere, and a hand-run `rm -rf` /
  `git worktree remove` leaves nothing either. Exactly the gap the ledger closes.
- **Relevant detail**: the #5919 session was never daemon-dispatched (no sweep
  for it in the log), so it held no `.loom/locks/issue-5919/` claim-lock — the
  one guard that would have made aggressive mode's `active_shepherd` check fire.

So the mechanism remains formally unproven, and the fix is deliberately two
things rather than one: the issue-open gate closes the one path that was
*structurally capable* of it, and the ledger makes the next occurrence a single
`grep` instead of another post-mortem.

### `loom-clean` / `cleanup.sh` / `loom-recover-orphans` fail on a stale binary (#4384)

**Symptom**: one of the three commands below fails outright instead of doing
anything — either with a `No module named loom_tools.clean` traceback (an
old pip-installed console script), or with an explicit `ERROR clean.sh: … does
not support the 'clean' subcommand (stale build)` from the wrapper.

**Root cause**: all three are now thin front-ends for native `loom-daemon`
subcommands (#4272 / PR #4301, commit `dba33666`):

| Command / wrapper | Native subcommand |
|---|---|
| `loom-clean`, `./.loom/scripts/clean.sh` | `loom-daemon clean` |
| `./.loom/scripts/cleanup.sh` | `loom-daemon cleanup logs` |
| `loom-recover-orphans`, `./.loom/scripts/recover-orphaned-shepherds.sh` | `loom-daemon recover-orphans` |

The same commit deleted the Python implementations (`loom_tools/clean.py`,
`cleanup.py`, `orphan_recovery.py`) and their console-script entry points, so
**there is no fallback**: a `loom-daemon` binary built before `dba33666` leaves
no working path at all. The wrappers capability-probe the binary and now fail
loudly with the remedy rather than degrading into a traceback.

**Check whether your binary is the problem**:

```bash
loom-daemon --version          # note the commit
loom-daemon clean --help       # "error: unrecognized subcommand 'clean'" => stale
```

**Remedy — rebuild or update `loom-daemon`, then retry**:

```bash
cargo build --release -p loom-daemon        # source checkout
./.loom/scripts/cli/loom-daemon-update.sh   # installed host (self-update)
```

Use `loom-daemon clean --force` / `loom-daemon recover-orphans --recover`
directly in the meantime — the native subcommands take the same flags.

**If a stale pip-era shim is shadowing the current one**: the installer writes
`loom-clean` / `loom-recover-orphans` shims next to the provisioned
`loom-daemon` (usually `~/.local/bin`). A pre-#4301 pip/homebrew install can
leave `from loom_tools.clean import main` shims earlier on `PATH` that will
never work again. Confirm with `command -v loom-clean` and remove them (e.g.
`pip uninstall loom-tools`, or delete the stale shim) so the daemon-backed one
resolves.

**Eleven other `~/.local/bin/loom-*` names have no daemon-backed replacement
at all** (`loom-agent-monitor`, `loom-auto-merge`, `loom-baseline-health`,
`loom-check-completions`, `loom-cleanup`, `loom-daemon-diagnostic`,
`loom-forge`, `loom-health-monitor`, `loom-status`, `loom-stuck-detection`,
`loom-worktree`) — their loom-tools console scripts were retired without a
loom-daemon subcommand to shim to, so a dangling symlink under any of these
names is permanently dead, not repairable (#5738; see
`docs/migration/daemon-state-consumers.md` for the per-name disposition).
Both `scripts/install/provision-daemon.sh` (every install/reprovision) and
`scripts/uninstall-loom.sh` (Step 5b) now remove these automatically when
they find one — scoped to a symlink whose target resolves through a
`loom-tools` path segment and no longer exists, so a same-named script you
authored yourself is never touched. No manual action needed on either path.

### `rm` of the installed `loom-daemon` binary is denied in an agent session (#5675)

**Symptom**: a self-build/reinstall verification step run from an agent session
— `rm -f /opt/homebrew/bin/loom-daemon`, `rm -f /usr/local/bin/loom-daemon`,
`rm -f ~/.local/bin/loom-daemon`, typically followed by an `ls` of the same path
to confirm the rebuild replaced it — is blocked with:

```
BLOCKED: rm target outside repo scope (LOOM_RM_SCOPE=repo): /opt/homebrew/bin/loom-daemon
```

(pattern `rm-scope-outside-repo`; observed once on 2026-08-07 and triaged under
the #3898 guard-decision telemetry policy).

**This is correct and stays that way.** The path is outside the repo/worktree
scope and on no ephemeral allowlist, so the repo-scoped `rm` guard denies it.
The trigger was reviewed and **kept flagged** rather than allowlisted: no
supported Loom flow needs to delete the installed binary, so allowlisting
Loom's own install paths would widen an outside-repo delete for no gain. See
`defaults/docs/guard-hooks.md` → "Repo-Scoped rm Guard" → "Removing an installed
`loom-daemon` binary — denied, and never necessary".

**Remedy — overwrite, don't delete**:

```bash
loom update                                  # machine-level install (~/.local/bin)
./.loom/scripts/cli/loom-daemon-update.sh    # the same delegate, called directly
loom-daemon --version                        # baked-in commit confirms which build is live
```

Both paths provision with `install -m 755` (falling back to `cp -f` +
`chmod 755`) straight over the existing file — the binary is replaced in place,
so there is never a delete step to run. If your installed binary is **not** at
the machine-level default, point the updater at it instead of removing it:

```bash
LOOM_DAEMON_BIN=/opt/homebrew/bin/loom-daemon ./.loom/scripts/cli/loom-daemon-update.sh
# or: LOOM_DAEMON_BIN_DIR=/opt/homebrew/bin ...
```

**If what you actually need is to remove a shadowing binary** — a second
`loom-daemon` earlier on `PATH` than the one Loom provisions (the #4079
stale-entry-point shape that the previous entry describes for `loom-*` shims) —
that is an **operator** action, not an agent one. `loom-daemon-update.sh` warns
about each such entry point on every run and deliberately deletes nothing. Do
the removal yourself in a plain shell outside the hooked agent session; a
headless agent has no sanctioned way through this deny, and the script-file
workaround is an unsanctioned guard bypass (`defaults/docs/guard-hooks.md` →
"When a Legitimate Operation Is Pattern-Blocked").

### Corrupted local git identity (`...github.comecho`, "cannot overwrite multiple values") (#4369)

**Symptom**: `git config user.email <value>` fails with `error: cannot
overwrite multiple values`, or a commit/merge ships with a garbled author
email like `loom-reviewer@users.noreply.github.comecho`.

**Root cause**: a now-deleted Tauri-era code path once pushed two shell lines
into an agent's terminal to set a per-role git identity — `git config
user.email "<email>"` immediately followed by an `echo "✓ Git identity
configured..."` line. A lost newline between the two glued the `echo`
token onto the email, corrupting it, and because `git config user.email
<v>` *replaces* rather than appends, repeated writes/`--add` improvisation
could also stack multiple values for the same key. That code was removed in
`d61acab0` (#3353) — **nothing on `main` writes `user.email`/`user.name`
anymore** — but the corrupted/stacked values persist as residue in any
checkout that predates the removal, and worktrees inherit them from the
parent repo's shared `.git/config`.

**Detect it**:

```bash
./.loom/scripts/check-git-identity.sh
```

This reads LOCAL (repo + per-worktree) scope only — never your global
identity — so a normal setup with no repo-local override never
false-positives. It exits `0` when clean, `1` (warning) when it finds
plain stacked values with no corruption, and `3` (hard fail) when it finds
the glued-token corruption pattern. `./.loom/scripts/worktree.sh` runs this
check automatically at worktree creation: it hard-fails on the corruption
pattern (a garbled commit author would otherwise ship silently — see PR
#4303) and warns-but-proceeds on a plain multi-value.

**Fix it** — preferred, falls back to your global identity (Loom no longer
sets a per-role local identity, so the local values are pure residue):

```bash
git config --unset-all user.email && git config --unset-all user.name
```

**Alternative** — keep one specific value (e.g. no global identity is
configured on this host):

```bash
git config --replace-all user.email <value-to-keep>
git config --replace-all user.name  <value-to-keep>
```

After either fix, re-run `./.loom/scripts/check-git-identity.sh` to confirm
it now reports clean, and verify a new commit picks up the intended author
with `git commit --allow-empty -m test && git log -1 --format='%an <%ae>'`
(then drop the test commit).

### Labels out of sync

```bash
# Re-sync labels from configuration
gh label sync --file .github/labels.yml
```

Label sync is a manual/install-time step (`./scripts/install/sync-labels.sh .`),
not something CI re-applies when `.github/labels.yml` changes. If a label is
defined in `labels.yml` but missing from the live repo, applying it fails with
`failed to update 1 issue` (the standard `gh` error for "label does not exist").
Run the sync script — or create the one label directly — to reconcile:

```bash
gh label list --search operator                      # empty => not provisioned
gh label create "loom:operator-only" --color F97316 \
  --description "Requires human action or ruling outside automation (creds, infra, hardware); sweep skips"
```

**GitHub caps label descriptions at 100 characters.** A `labels.yml` entry with a
longer description fails to sync (HTTP 422 "description is too long") and the label
silently never gets created. Keep descriptions at or under 100 chars.

#### `loom:blocked` vs `loom:operator-only` vs `loom:needs-capability`

These status labels look similar but mean different things to the automation:

- **`loom:blocked`** — work is *automatable* but currently waiting on a dependency
  (another issue, an unmerged PR, missing context). The intent is "unblock it, then
  a Builder can proceed."
- **`loom:operator-only`** — work requires a *human to act or rule outside
  automation entirely* (rotating credentials, infra changes, hardware access,
  manual deploys — or an owner-gated decision: an issue the code owner filed as a
  TODO on owner-tracked code, where the design direction is the owner's call).
  Sweep skips these in pre-flight rather than attempting them; a human must
  do the work off-automation before the issue can proceed.
- **`loom:needs-capability`** (#5817) — a narrower claim than `loom:operator-only`:
  blocked on a missing tool/agent capability, not an operator-by-right decision.
  Sweep skips these identically to `loom:operator-only` in pre-flight today; the
  filed capability-request issue should be linked (`Depends on #N` / `Requires
  #N`). See `.loom/docs/label-state-machine.md` § "`loom:needs-capability` — a
  narrower claim than `loom:operator-only`" for the full split rationale.

Reaching for `loom:blocked` when you mean `loom:operator-only` (or
`loom:needs-capability`) conflates "waiting on a dependency" with "needs a human
action outside automation," which muddies the daemon/sweep skip semantics. Use
`loom:operator-only` for the human-must-act-off-automation case, and
`loom:needs-capability` specifically when the blocker is missing tooling rather
than a human ruling.

### An operator edit to `.loom/config.json` disappeared (#4641)

`.loom/config.json` has exactly one production writer: `loom-daemon init` (run by
`install.sh` reinstalls and by the `fleet add-worker` provisioning steps).
`resync-installed.sh` and `loom-daemon calibrate --write` never touch it.

`init`'s merge is **existing-wins** — consumer keys, including ones absent from the
shipped template such as `autonomous.workFinder.maxConcurrent`, survive — so a
tuned value vanishing means one of the other branches fired. Every `init` call now
emits one greppable line naming the branch it took:

```bash
grep 'init: config.json:' ~/.loom/daemon.log
```

| Branch | Level | Meaning |
|--------|-------|---------|
| `fresh-write` | `info` | No config existed; the template was written verbatim. |
| `merge-preserved` | `info` | Existing values kept. Carries a per-key diff (`+ added`, `~ changed`, `- dropped`) or "no effective config change". |
| `template-invalid-skip` | `warn` | The shipped `defaults/config.json` is unparseable; your file was left untouched (and got no template updates). |
| `invalid-JSON-fallback-overwrite` | `warn` | **Your config was replaced by the template.** The line names the discarded keys. |

A `~` or `-` entry on `merge-preserved` is unexpected under existing-wins semantics
and is worth investigating.

The fallback branch only fires when the file on disk does not parse as a JSON
object (a torn write from a concurrent writer, a hand-edit with a syntax error, or
a top-level array). Before overwriting, `init` copies the unparseable bytes to
`.loom/config.json.bak` — recover your tuned keys from there:

```bash
cat .loom/config.json.bak
```

`.loom/*.bak` is git-ignored, and `fleet add-worker` no longer re-runs
`loom-daemon init` against a workspace that already has a `.loom/config.json`, so
repeat provisioning passes cannot re-enter this path on a tuned host.

### Conflict markers left in `.loom/config.json` after a `git stash pop` (#6499)

`.loom/config.json` is **tracked**, and every existing repo carries at least
one host-specific field patched locally in the working tree (e.g.
`safehouse.socket`, `observability.ingestKeyFile`, a stale `room` value —
#5457 is the durable fix that will stop this). Because that patch sits
uncommitted on top of a tracked file, a `git stash push`/`git stash pop`
cycle against the primary checkout (whether run by a human or an agent) can
conflict on it — and if that conflict is left unresolved, `.loom/config.json`
ends up on disk with literal `<<<<<<<` / `=======` / `>>>>>>>` markers
embedded in it. That is invalid JSON: `config_resolver::resolve_effective_config`
silently falls back to `{}` for this tier, and the daemon runs on **built-in
defaults** for every value that file carried — `observability`, `safehouse`,
`autonomous.roleRunner`, and any other block your host's config tuned,
disabled or reconfigured without you doing anything.

**Check first, always:**

```bash
jq . .loom/config.json
```

A parse error there is definitive — jq's own error message names the exact
line/column, and the daemon's own boot log carries the same diagnosis at
`ERROR` (not a buried `WARN`) as of #6499:

```bash
grep 'config_resolver:.*is unreadable/malformed' ~/.loom/daemon.log
```

The daemon's periodic primary-checkout pass (`primary_checkout_reaper`, on by
default) also logs an `ERROR` line naming the unmerged path(s) — e.g.
`UU .loom/config.json` — on every tick the condition persists, independent of
a restart, whenever the abandoned-conflict shape (unmerged index entries with
no merge/rebase actually in progress) is present; see `grep
'ABANDONED CONFLICT STATE'` / `primary_checkout_reaper:` in the same log.
`check-main-clean.sh` (the Builder-workflow-invoked counterpart, #6162 AC3)
reports the identical condition — `ABANDONED CONFLICT STATE` — for any
tracked file, not just `.loom/config.json`.

**Once the markers are committed, every detector above goes blind.** All three
key on an *unmerged index entry*, and `git add` clears that — so a
`chore: resync installed Loom surfaces` pass that sweeps the corrupted file
into a commit makes the corruption invisible to them while leaving it live in
the tree (exactly how the #6499 markers reached `main`). The gate for that
case is content-level, not index-level:

```bash
./.loom/scripts/check-conflict-markers.sh          # scan every tracked file
./.loom/scripts/check-conflict-markers.sh --dir .  # or a directory, recursively
```

It exits `2` and names each offending path with its marker line numbers.
Detection is extension-agnostic (the gap that let a `.json` file past
`check-shell-syntax.sh`'s `*.sh`-only `bash -n` gate, #6162) and keys only on
line-start `<<<<<<< ` / `>>>>>>> `, so a markdown setext heading underline, a
`=======` separator comment, and inline backticked marker text in prose (as
in this very section) are all left alone. A fixture that genuinely must
contain markers opts itself out by embedding the literal string
`check-conflict-markers:allow` in its own content. This runs in CI on every
push and PR, unfiltered by changed-path group.

**Resolution is mechanical — keep the local host's own values:**

1. Open the file and find the conflict hunk(s):
   ```bash
   grep -n '^<<<<<<<\|^=======\|^>>>>>>>' .loom/config.json
   ```
2. For each hunk, keep **this host's** side (the values already in local use
   on this machine — the socket path, ingest key file path, room, etc. that
   match this host's own filesystem layout) and delete the markers and the
   losing side entirely. There is no "correct" side in the abstract; the
   correct side is whichever one this host was actually running with.
3. Verify the fix parses:
   ```bash
   jq . .loom/config.json
   ```
4. If the file is (or was) genuinely unmerged in git's own index — `git
   status --porcelain` shows a `UU` (or `DD`/`AU`/`UD`/`UA`/`DU`/`AA`) line for
   it, not just modified — clear that stage-conflict state too, e.g. `git add
   .loom/config.json` once the content is fixed, or discard the whole
   conflicted stash-pop attempt with `git reset --merge` and reapply your
   local patch by hand from memory/backup.
5. Restart the daemon (`loom-daemon restart`, or however this host manages
   it) so it picks up the corrected file — the process that hit the parse
   failure keeps running on defaults for its own lifetime; fixing the file on
   disk alone does not retroactively fix an already-running process.

If you are unsure which side is correct, do **not** guess — the two Mac
hosts in the original #6499 incident had *different* correct answers (one
Mac path, one Linux path) for the same key, because the "conflict" was really
two different hosts' legitimate local patches colliding via a shared stash.
A backup of the pre-conflict file (if one exists, e.g. an untracked
`.loom/config.json.bak-conflict-<date>` sitting alongside it) is the most
reliable source of truth for what this host's own values were.

### `install.sh` refuses to run: "Another Loom install is already running" (#4928)

`install.sh`'s `--quick` / `--clean` paths take a per-target lock at
`<target>/.loom/.install.lock` before any destructive phase, because two
installers racing over one target interleave one run's uninstall (which stages
Loom file deletions and strips the Loom sections out of `CLAUDE.md` /
`.gitignore` **in place**) with the other's copy phase. The message names the
owning PID, host, and phase:

```bash
cat <target>/.loom/.install.lock   # pid / host / started / phase
```

- **The PID is alive** — a real install is in flight (a `cargo build --release`
  can run for minutes; it emits a progress line every 15s). Wait for it.
- **The PID is gone** — the next installer reclaims the lock automatically; you
  should never need to delete it. If you do (e.g. a lock written by another
  host, which cannot be liveness-probed and is only reclaimed after
  `LOOM_INSTALL_LOCK_MAX_AGE`, default 6h), `rm -f <target>/.loom/.install.lock`.

If the lock's `phase` is `uninstalling` / `installing` / `restoring`, that run
died **inside the destructive window** and the target may be partially
uninstalled. The next installer prints the recovery commands; the short form is:

```bash
git -C <target> status --short
git -C <target> restore --staged --worktree -- .loom .claude CLAUDE.md .gitignore
git -C <target> stash list | grep loom-install   # changes the installer stashed, if any
```

### Daemon won't start

```bash
# Check daemon logs
tail -f ~/.loom/daemon.log
```

### Claude Code not found

```bash
# Ensure Claude Code CLI is in PATH
which claude

# Install if missing (see Claude Code documentation)
```

### `loom-daemon: command not found` over plain ssh (#5393)

```
$ ssh loom-worker-2 'loom-daemon workspace list'
bash: line 1: loom-daemon: command not found
```

`loom-daemon` is installed at `~/.local/bin/loom-daemon`, which is added to PATH
by your **login shell's rc file**. `ssh host <cmd>` runs a *non-login,
non-interactive* shell that never sources that rc file, so `~/.local/bin` is not
on PATH and the bare name does not resolve. (The same mechanism produces the
false "missing dependency" from `install.sh` — see [`install.sh` reports a
dependency that is installed](#installsh-reports-a-dependency-that-is-installed-5393)
below.)

Three supported ways to drive `loom-daemon` over ssh, in order of preference:

1. **Source the login profile** so PATH is populated exactly as it is
   interactively:

   ```bash
   ssh loom-worker-2 'bash -lc "loom-daemon workspace list"'
   ```

2. **Call the fixed install location** directly — the machine-level install path
   is stable, so no PATH is needed:

   ```bash
   ssh loom-worker-2 '~/.local/bin/loom-daemon workspace list'
   ```

3. **Let Loom's own scripts resolve it** — every in-tree caller sources
   `defaults/scripts/lib/locate-daemon-bin.sh` (`loom_locate_daemon_bin`), which
   already probes `$LOOM_DAEMON_BIN` → PATH → `${LOOM_DAEMON_BIN_DIR:-$HOME/.local/bin}`
   → repo-local build output (#4875). Point new fleet automation at that helper
   rather than reimplementing per-caller path probing.

### `install.sh` reports a dependency that is installed (#5393)

```
$ ssh loom-worker-1 'cd ~/GitHub/loom && ./install.sh --quick -y ~/GitHub/repo'
✗ Error: Missing required dependencies: pnpm cargo -- cannot continue ...
```

Same root cause as the daemon case above: over a non-login ssh shell, PATH lacks
the per-user install roots (`~/.cargo/bin`, `~/.local/bin`, `/opt/homebrew/bin`,
…), so tools that are installed and runnable look absent. `install.sh` now
probes those roots directly: a tool found there is used (its directory is added
to PATH for the rest of the install) and reported with a `not on this shell's
PATH` warning rather than as missing. Only tools that are absent from **every**
probed root are treated as genuinely missing — a distinction that matters
because the two need different fixes (install the tool vs. fix PATH). If you
prefer to fix PATH once up front, run the whole install under a login shell:

```bash
ssh loom-worker-1 'bash -lc "cd ~/GitHub/loom && ./install.sh --quick -y ~/GitHub/repo"'
```

### Sweep output invisible when invoked with `2>&1`

When `claude -p "/loom:sweep N"` is run with `2>&1` redirection (e.g., from Claude Code's Bash tool for long-running processes), output may be silently dropped. This is because the Bash tool's capture buffer can be exhausted by a long-running child process when both stdout and stderr are forced through the same pipe.

**Workaround** — use a file redirect:

```bash
# Redirect to file, then cat the result
claude -p "/loom:sweep 123" --dangerously-skip-permissions > /tmp/sweep-123.log 2>&1
cat /tmp/sweep-123.log
```

**Built-in log file** — when a sweep child runs, it automatically tees all output to `.loom/logs/sweep-issue-N.log`. If output is invisible in your terminal, check this log file:

```bash
cat .loom/logs/sweep-issue-123.log
# or follow in real time:
tail -f .loom/logs/sweep-issue-123.log
```

### API Error: 400 due to tool use concurrency issues

This error occurs when Claude Code's parallel tool execution causes malformed API message structures. See the dedicated guide: [Tool Use Concurrency Errors](./tool-use-concurrency-errors.md)

**Quick recovery**:
```bash
# In Claude Code, run:
/rewind
```

**Prevention** - Add to `~/.claude/CLAUDE.md`:
```markdown
# Force Sequential Tool Execution
Execute tools sequentially, never in parallel.
Process one tool call at a time.
Wait for tool_result before initiating next tool execution.
```

**Common triggers**:
- Multiple parallel file operations (Read, Write, Edit)
- Using print mode (`-p` flag) instead of interactive mode
- PostToolUse hooks that interfere with message structure
- Editing files while they're open in an IDE

### Orphaned issues stuck in loom:building state

When an agent crashes or is cancelled while building, issues can get stuck in `loom:building` state without a PR. The shell script that historically handled this was deleted in `b811fca8` (#3433, "delete shepherd brain + /shepherd skill + milestone writers") during the v0.10.0 shepherd deprecation. Its successor, `loom-recover-orphans`, detects and recovers these:

```bash
# Check for stale building issues (dry run)
loom-recover-orphans

# Show detailed progress
loom-recover-orphans --verbose

# Auto-recover stale issues (resets to loom:issue)
loom-recover-orphans --recover

# JSON output for automation
loom-recover-orphans --json
```

`loom-recover-orphans` is a thin shim for `loom-daemon recover-orphans` — if it
fails with `No module named loom_tools.orphan_recovery` or a "stale build"
error, see [`loom-clean` / `cleanup.sh` / `loom-recover-orphans` fail on a
stale binary](#loom-clean--cleanupsh--loom-recover-orphans-fail-on-a-stale-binary-4384).

**Run it from inside the checkout** (or pass `--workspace <path>`): repo-root
resolution requires an ancestor holding **both** `.git` and `.loom/`, so a
machine-level `~/.loom` (the token pool) is never mistaken for a repository. Run
from anywhere else it exits `1` naming the directory it searched — it does not
guess (#5140).

**Exit codes**: `0` = assessed, nothing orphaned · `2` = orphans found in
dry-run mode · `3` = **could not assess** (e.g. the `gh issue list --label
loom:building` query failed). A `3` is never reported as "No orphaned tasks
found" — a failed query is not evidence that nothing is stranded, and `--json`
carries `assessment_failed` / `assessment_errors` for automation.

**What it does**:
- Finds issues with `loom:building` label that have been stuck
- Checks if there's an associated PR (by branch name or body reference)
- Issues without PRs older than threshold are flagged/recovered
- Issues with stale PRs are flagged but not auto-recovered (need manual review)

### `loom:building` left on a CLOSED issue (#6199)

The above covers a stuck OPEN issue. A different, purely cosmetic case: a
**closed** issue that still carries `loom:building` — `gh issue list --label
loom:building` without `--state open` returns these as noise. `merge-pr.sh`
strips the label from any issue its own merge closes (`Closes #N` / `Fixes
#N` / `Resolves #N`), but issues closed by other means (manually, as a
duplicate, or `--reason "not planned"`) are not covered automatically —
run `./.loom/scripts/clean-stale-building-labels.sh [--repo OWNER/NAME]
[--dry-run]` to sweep those (idempotent, safe to re-run).

### Uncommitted work in the primary clone can be quarantined at any time — branching does not protect it (#5194)

**Symptom**: uncommitted edits made directly in a Loom-managed repo's **primary
clone** (the main checkout, not a `.loom/worktrees/issue-N` linked worktree)
disappear. This happens even when the edits were made on a feature branch, not
on `main` — checking out a branch first feels like it should be "safe," but it
is not.

**Root cause**: `check-main-clean.sh --quarantine` (see the Wave Lifecycle
"Backstop" step in `defaults/.claude/commands/loom/sweep.md`) polices the
**primary clone's working tree as a whole**, keyed on `git rev-parse
--show-toplevel`/`--git-common-dir`, not on the currently checked-out branch.
A concurrent `/loom:sweep` (interactively, via the daemon, or via cron) that
snapshots the primary clone and later finds it dirty will stash-quarantine
**every** offending path in one `git stash push --include-untracked`, whichever
branch happens to be checked out at that moment. It has no way to know "this
dirt is on a branch I don't own" — from its point of view, any uncommitted
change in the primary clone that was not there at snapshot time is
contamination, full stop.

**Branching is NOT sufficient protection.** This is the first (and wrong)
inference people make: "I'm not on `main`, so a sweep can't touch my WIP."
Twice in one session, uncommitted work in the primary clone was lost this way
even though it lived on a dedicated branch — see #5185 and
[rjwalters/repo#89](https://github.com/rjwalters/repo/issues/89) for the
incidents that prompted this note. The quarantine backstop exists precisely
because *something* wrote to the primary clone's working tree outside of a
worktree — it does not, and should not, special-case "but it's on a branch."

**The only patterns that actually protect uncommitted work**:

1. **Create a worktree outside the policed clone** and do the work there:
   ```bash
   git worktree add /path/outside/the/clone some-branch
   ```
   A sibling directory (or anywhere off the main checkout's `git
   rev-parse --show-toplevel`) is never touched by `check-main-clean.sh`,
   because it only ever inspects the primary clone's own working tree. This is
   exactly what `./.loom/scripts/worktree.sh <issue-number>` gives you under
   `.loom/worktrees/issue-N` — use it (or a hand-rolled `git worktree add`
   pointed elsewhere) for any WIP you want to survive a sweep, rather than
   editing directly in the primary clone.
2. **Commit before a sweep can fire.** A commit is not "dirt" — the backstop
   only quarantines the working tree's uncommitted delta against its snapshot
   baseline, so committed history on any branch is unaffected regardless of
   which branch is checked out when a sweep runs.

Checking out a branch and leaving changes **uncommitted** in the primary clone
protects against neither: the working tree is still what gets swept into a
stash rescue ref, and recovering it means digging through `git stash list`
in the primary clone (`stash_ref`/`stash_commit` are logged to
`.loom/logs/main-quarantine.log`) rather than simply finding your branch
intact.

**Discovering an outstanding quarantine without prior knowledge that one
occurred (#5185).** Both #5185 incidents above were noticed only by chance —
an unrelated hygiene command happened to count stashes and flag one that had
not existed at session start. `git stash list` and the structured
`.loom/logs/main-quarantine.log` are both authoritative, but neither is
something an operator thinks to check unprompted. `/loom:sweep` now runs
`./.loom/scripts/check-quarantine-stashes.sh` before its first wave (see
"Outstanding Quarantine Stashes" in `defaults/.claude/commands/loom/sweep.md`)
— a non-blocking, read-only advisory that lists every outstanding
`loom-quarantine:` stash (its `stash@{N}` selector, age, and run/issue label)
whenever one exists. Run it manually at any time to check without waiting for
a sweep:
```bash
./.loom/scripts/check-quarantine-stashes.sh
```

**A note on the quarantine's stash message**: `check-main-clean.sh` passes an
explicit `-m "loom-quarantine: $QUARANTINE_LABEL"` message to `git stash
push`, but `git stash list` always prefixes stash entries with `On
<branch>:` regardless of the `-m` message supplied — so a quarantined stash
reads as e.g. `On some-branch: loom-quarantine: run=... issue=...`, which can
itself read as "this was scoped to `some-branch`" even though the quarantine
is whole-working-tree, not branch-scoped. Changing that format is out of
scope here — `stash_message` is also emitted as a structured field in the
`main-clean.quarantine` JSON log line, and other tooling may parse either
form — but if you are debugging a quarantine, read the `On <branch>:` prefix
as "which branch happened to be checked out at quarantine time," not as
"which branch's changes were protected."

### Finding outstanding quarantine stashes (#5185)

**Symptom**: none — that is the problem. A quarantine is loud at the moment it
happens (a `main-clean.quarantine` line on stderr and in
`.loom/logs/main-quarantine.log`) and silent forever afterwards. Nothing drops
the rescue stash, nothing reminds anyone it exists, so entries pile up: 29 on
one host, oldest 7 days; five in a consumer repo inside 24 hours, none
reconciled. They were noticed only because an unrelated hygiene command
happened to count stashes.

**List them**:

```bash
./.loom/scripts/check-main-clean.sh --list-quarantined          # human-readable
./.loom/scripts/check-main-clean.sh --list-quarantined --json   # machine-readable
```

The same count (and the newest few entries) appears as a `Quarantined work`
section in `./.loom/bin/loom status`, so an outstanding quarantine is
discoverable without knowing one happened. The report is read-only — it never
pops, drops, or reorders anything — and exits 0 whether or not anything is
outstanding.

It covers every Loom producer that pushes to the stash stack, not just
`check-main-clean.sh --quarantine`'s `loom-quarantine:` entries (the Auditor's
`auditor-tmp-drift-stash-<epoch>` shelf is the other one), and flags any entry
that captured **nothing** — those are pure noise and safe to drop.

**Reconciling an entry**:

1. **Identify by commit, not by index.** `stash@{N}` shifts every time anything
   pushes, and `refs/stash` is one stack shared by the primary clone and every
   linked worktree — so the index you read a minute ago may name a different
   entry now. The report prints each entry's commit sha for exactly this reason.
2. **Read it**: `git -C <main> stash show -p --include-untracked <commit>`.
3. **Check liveness before dropping anything.** Each quarantine entry carries
   `run=sweep-<...>` and `issue=<N>`. An entry naming a **finished** run whose
   issue is **closed** is almost certainly superseded; one naming a live sweep
   or an open issue may be the only copy of that work. This is the judgement a
   human cannot make by eye across dozens of entries, and it is why bulk
   "prune anything that looks stale" is unsafe on a busy host.
4. **Replay, don't pop.** To recover the work, apply the diff **inside the
   owning issue worktree** rather than `git stash pop`-ing it back into the
   primary clone — a pop in a shared stack can restore someone else's entry
   (see "Never use bare `git stash` for ad-hoc WIP" in `builder.md`), and it
   puts the contamination straight back where the backstop will quarantine it
   again.

**Empty entries**: an entry flagged `[EMPTY]` recorded nothing. These came from
a race between the contamination snapshot and the stash push; the push is now
preceded by a fresh re-derivation of the offending paths, so new ones should
not appear (and a quarantine with nothing left to rescue logs
`"result":"no_op"` and creates no stash at all). Existing empty entries carry
no work and can be dropped once you have confirmed the flag.

### Retiring quarantine stashes safely (#5693)

Step 3 above — "check liveness before dropping anything" — is the judgement
that does not scale: a fleet audit found **148 stashes across three hosts in
twelve days**, of which exactly **one** held unlanded engineering content, and
finding it took an hour of hand triage. `loom-daemon stashes` mechanises that
triage.

```bash
loom-daemon stashes list                     # classify, read-only, never drops
loom-daemon stashes list --paths             # + the per-path proof for every file
loom-daemon stashes retire                   # same thing — still a dry run
loom-daemon stashes retire --execute         # the only invocation that drops
loom-daemon stashes retire --issue 123 --execute   # scoped to one issue's stashes
```

A stash is retirable only when **both** independent conditions hold — never
either alone:

1. **Provenance**: the issue named by the `loom-quarantine:` label is CLOSED.
2. **Content**: *every* path in the stash is provably recoverable without it —
   its blob is identical to `HEAD`'s, or identical to a commit reachable from
   `HEAD` (the "superseded local copy" case: the work landed and was then built
   on further), or it is installer-managed/regenerable (the same
   `is_ignorable_dirt` classes the main-health gate uses, #4332/#3950/#4239),
   or it is a machine-generated artifact (`__pycache__/`, `.venv/`,
   `node_modules/`, `*.egg-info/`, `*.pyc`, …).

Everything else is kept: an open issue, a missing `issue=` token, a forge
lookup that failed, a `git` failure, a stash that *deletes* a file, a brand-new
untracked source file, and — critically — a stash that is 90 % superseded and
10 % real. `git stash drop` is all-or-nothing, so one unproven path holds the
whole entry back. A closed issue is **not** sufficient on its own; that is
precisely the shape of the one stash in 148 that mattered.

**Notes**

- Nothing is dropped without `--execute`. There is no config flag, cadence, or
  daemon timer that drops a stash — it is an explicit operator action only.
- Every drop is journaled to `.loom/logs/stash-retirement.log` **before** it
  happens, recording the stash's commit sha. A dropped stash commit survives in
  the object database as an unreachable object until gc, so
  `git stash apply <sha>` (or `git show <sha>^3:<path>` for a file that was
  untracked) still recovers it.
- The operation is idempotent: re-running it after a drop, or against a stash
  another host already dropped, is a no-op, not an error. Selectors are
  re-resolved from each entry's commit sha immediately before the drop, because
  `refs/stash` is one stack shared by every worktree and indices shift under
  you.
- It only ever considers `loom-quarantine:`-labelled entries. An Auditor drift
  shelf, a Judge park stash, or an ad-hoc `git stash` is never a candidate.

### Taking a stash back off the stack without leaving conflict markers (#6501)

**Never run a bare `git stash pop` in the primary checkout.** Use the verified
wrapper instead:

```bash
./.loom/scripts/safe-stash-pop.sh                      # pop stash@{0} in the current repo
./.loom/scripts/safe-stash-pop.sh --repo /path/to/repo 'stash@{2}'
./.loom/scripts/safe-stash-pop.sh --dry-run            # preconditions + target, no mutation
./.loom/scripts/safe-stash-pop.sh --no-restore         # keep a conflicted tree to resolve by hand
./.loom/scripts/safe-stash-pop.sh --json --quiet       # one structured line for a script
```

**Why.** `git stash pop` is not atomic. When its 3-way merge conflicts it writes
`<<<<<<< Updated upstream` / `=======` / `>>>>>>> Stashed changes` into the
affected **tracked** files, leaves unmerged entries in the index, exits non-zero
— and stops. Nothing verifies the result. If the caller does not read the exit
status, the primary checkout is left in an *abandoned conflict state* that looks
like ordinary dirt, and the next `git add -A && git commit` ships the markers.
That is exactly how commit `7d169a06` landed a `.loom/config.json` containing a
live conflict-marker block, silently breaking the daemon's config parse
fleet-wide (#6499 / #6502).

**What the wrapper guarantees.** Exactly one of these outcomes, always:

| Exit | Meaning |
|------|---------|
| `0` | Popped and **verified** clean — no unmerged entries, no conflict markers. Entry consumed, as with a normal pop. |
| `1` | Precondition failure — not a repo, unborn `HEAD`, a merge/rebase/cherry-pick already in progress, an index that *already* has unmerged entries, or a dirty tree that could not be snapshotted. **Nothing ran.** |
| `2` | No stash entry at the given ref — nothing to do. |
| `3` | The pop conflicted; the pre-pop working tree was **restored and verified**, and the stash entry is preserved. |
| `4` | The pop conflicted and the pre-pop state could **not** be safely restored. The tree is left exactly as `git` left it and every recovery handle is named. |
| `5` | The pop conflicted and `--no-restore` was given — the conflicted tree is left in place deliberately. |

**Nothing is ever discarded.** The rollback runs only when the stash entry is
confirmed still on the stack, and the pre-pop tree is captured first as a
`git stash create` commit anchored under `refs/loom/safe-stash-pop/<stamp>` —
never `refs/stash`, so it cannot collide with another worktree's stack (the
#4821 hazard). If either precondition cannot be met the wrapper reports loudly
instead of rolling back: markers in a tracked file are recoverable, destroyed
WIP is not. On exit `3` the snapshot ref is kept as insurance; delete it with
`git update-ref -d <ref>` once you are satisfied.

**Already committed markers?** That is the recovery case, not the prevention
case: `git grep -n '^<<<<<<< '` across the checkout finds them, and
`./.loom/scripts/check-main-clean.sh` reports an unmerged index entry with no
merge in progress as its own distinct, more urgent failure (see its
"Abandoned-conflict detection" block). Resolve or `git merge --abort` before
running anything else — the wrapper deliberately refuses to pop on top of a
pre-existing conflict state (exit `1`).

**Related tools.** Inside an issue worktree, a Builder's own WIP should use
`./.loom/scripts/worktree.sh stash-push <N>` / `stash-pop <N>`, which anchor to
a per-issue ref and never touch `refs/stash` at all. `check-main-clean.sh
--quarantine` moves contamination *onto* the stash stack; `safe-stash-pop.sh` is
the safe way back off it. `guard-destructive-generic.sh`'s
`stash-scope:main-checkout` ask names the wrapper in its message.

## Several unrelated things hang at once (macOS Gatekeeper / `syspolicyd`)

**Symptom:** several unrelated processes — a `cargo` build, a sweep child, a
shell wrapper, a timing-sensitive test — appear to hang *simultaneously*, all
sitting at **zero CPU time** even after minutes of wall-clock. It looks like
host-wide CPU starvation, but the CPUs are idle.

**Zero CPU means nothing on its own.** Before chasing any single victim,
distinguish the two very different conditions that both present as "stuck at zero
CPU":

- *Many* unrelated processes at zero CPU, including short-lived `exec`s that never
  make progress → **macOS Gatekeeper / `syspolicyd` saturation** (this section).
- A *single sweep* at zero CPU with a flat log → almost certainly **healthy, not
  wedged**. Sweeps are network-bound and accrue almost no CPU while awaiting API
  responses; `%CPU`, cumulative `TIME`, a `sample <pid>` stack (which parks in
  `CFRunLoopRun → mach_msg`, indistinguishable from a block), and sweep-log size
  **cannot** tell a working sweep from a dead one. Verify liveness via the side
  effects a sweep actually produces — `ls .loom/worktrees/`, `git log` in the
  worktree, `git ls-remote --heads origin 'feature/issue-*'`, and the forge
  (`gh pr list`) — before concluding anything. (See "Stuck Agent Detection" below.)

### First diagnostic move

```bash
ps -axo %cpu,time,comm | sort -rn | head
```

If `syspolicyd` is at or near the **top** of that list, stop debugging the
individual victims — they are symptoms, not the cause. Under parallel `cargo`
builds, macOS's Gatekeeper daemon (`syspolicyd`) becomes the bottleneck: it
serializes code-signature / notarization checks on every `exec`, and when
saturated it stalls *every* new process launch host-wide.

### The exec-stall signature

A victim of `syspolicyd` saturation is a process caught mid-`exec` — launched, but
its target binary has not yet cleared Gatekeeper. Its tell-tale signature:

- **Zero CPU time** accumulated even after minutes of elapsed wall-clock.
- **`STAT S`** (interruptible sleep) — blocked, not spinning.
- **No child processes** — the wrapper never got far enough to fork its target.
- `lsof -p <pid>` shows `txt = /usr/bin/env` (or another wrapper) that **never
  exec'd its actual target** — the process is frozen inside the launch, waiting on
  the signature check.

### It is load-induced and self-recovering

The condition is caused by launch load, not by any one process, and it clears on
its own once the load is removed — **removing the build load (letting the parallel
`cargo` builds finish, or throttling them) is normally sufficient.** If you need to
unwedge it manually:

```bash
sudo killall syspolicyd
```

`syspolicyd` is a system daemon that `launchd` restarts immediately; killing it
drops its saturated in-flight queue and lets the stalled `exec`s proceed.

### Why this matters — a falsified contention story

This signature was originally *misdiagnosed* as CPU contention. The build gate's
timing-sensitive tests failed under host load and the failures were attributed to
sweeps starving the gate; #4044 / #4046 falsified that — the same tests passed
968/968 later with **no code change**, establishing the failures as `syspolicyd`
exec-latency artifacts rather than contention. The narrative and its consequences
for gate niceness live in [`build-gate.md`](build-gate.md) (the #4044 / #4046
falsification passage). ADR-0011 also records this as a macOS-specific defect that
is invisible to Linux CI by construction — see
[`docs/adr/0011-ci-runner-platform.md`](https://github.com/rjwalters/loom/blob/main/docs/adr/0011-ci-runner-platform.md)
(upstream Loom repo — not shipped to consumer installs).

> **Historical note on timeouts.** Contemporaneous incident writeups mention a
> "600s" build-gate budget; that figure is **incident history only**. #4048 raised
> the live budget to **1200s** — do not read 600s as the current value.

## Stuck Agent Detection

> **Note (post-#4274):** the Python `loom-stuck-detection` CLI was removed in
> epic #4081 phase 3. It read `.loom/spawn-loop-state.json::running[].last_heartbeat`,
> a file whose only writer (`spawn-loop.sh`) was deleted in v0.11.0 — so it had
> already been a safe no-op (report-only, never tears down work). Live sweep
> liveness is now owned by the Rust `loom-daemon` sweep registry.

### Check for stuck agents

The native surfaces replace the former CLI:

```bash
# Daemon + registry health summary (sweeps, PIDs, quarantine state)
loom health                # execs `loom-daemon status`
loom-daemon status --json  # machine-readable

# Per-sweep liveness (MCP): list sweeps and their last activity
#   mcp__loom__list_sweeps / mcp__loom__get_sweep_status
# plus the per-issue checkpoint timestamps under
#   .loom/sweep-checkpoint/issue-<N>.json
```

### Stuck indicators (post-v0.10.0)

| Indicator | Default Threshold | Description |
|-----------|-------------------|-------------|
| `stale_heartbeat` | 5 minutes | No checkpoint update for extended time |
| `dead_pid` | (instant) | PID in the daemon sweep registry is no longer alive |
| `error_spike` | 5 errors | Multiple errors in `.loom/logs/sweep-issue-N.log` |

The pre-v0.10.0 indicators `missing_milestone:worktree_created` and `extended_work` were retired when the Python daemon brain (`daemon_v2/`) was removed — see [the migration guide § Per-CLI breaking changes](https://github.com/rjwalters/loom/blob/main/docs/migration/v0.10.0-shepherd-deprecation.md#per-cli-breaking-changes) (upstream Loom repo — not shipped to consumer installs) for the field-level diff. The shell-level daemon surface (`./.loom/scripts/daemon.sh`) is preserved but does not write progress files, so milestone-based heuristics no longer apply.

### A killed Task/Agent-tool subagent leaves no teardown signal for external resources it held

**Symptom**: a Task/Agent-tool subagent dispatched mid-conversation (e.g. a
Builder or Judge subagent under `/loom:sweep`) is killed by a session cap or an
API error while it holds an external, non-Loom resource — a browser profile
lock, a hardware device claim, a DB advisory lock, a cloud lease, any arbitrary
mutex a caller's own tooling manages. The resource stays held indefinitely:
nothing in Claude Code notifies the parent session or Loom that the subagent
died, and nothing runs the subagent's own cleanup code.

**Root cause — no kill-time teardown hook exists for Task-tool subagents (as of
2026-08-04, this repo's Claude Code)**. This repo's `.claude/settings.json`
wires exactly four hook types: `PreToolUse`, `UserPromptSubmit`, `SessionStart`,
and `Stop` (the top-level `Stop` hook fires on the **outer** session ending, not
per-subagent). There is no `SubagentStop`-equivalent hook that fires when an
individual Task/Agent-tool dispatch is killed — a session-cap or API-error kill
of a subagent bypasses whatever teardown code that subagent would otherwise
have run on a graceful exit. **This was evaluated and found infeasible against
Claude Code's current hook taxonomy** (issue #5262) — if a future Claude Code
release adds a subagent-lifecycle hook, re-evaluate then; until it does, do not
re-propose a kill-time teardown hook or a daemon-side termination-signal
channel for Task-tool subagents without first confirming the taxonomy actually
changed upstream.

**Guidance for callers building agent-driven scripts against Loom**: because no
teardown signal is coming, any external lock a subagent might hold must be
designed so it recovers **without** relying on the holder's liveness or on any
kill notification:

- **Make locks self-expiring by wall-clock heartbeat, not by holder liveness.**
  A lock that is only released by its holder's own cleanup code stays held
  forever once that holder is killed outside its control (a session cap or API
  error gives it no chance to run cleanup at all). Instead, record a
  last-heartbeat timestamp and treat the lock as free once that timestamp is
  older than a bounded TTL — the same shape whether the lock lives in a file's
  mtime, a directory, a DB row, or a remote lease API.
- **Reap on a schedule, not only on the next acquire attempt.** A "stale-break
  on acquire" check (comparing an existing lock's age against a threshold only
  when something next tries to acquire it) never fires if nothing else ever
  tries to acquire that resource again — the lock silently stays stale
  forever. Pair it with a periodic reaper that proactively expires stale
  entries on its own cadence, independent of acquisition attempts.

**Loom's own internal precedent for this pattern** (worked examples, not a
reusable shared primitive external callers can import — they solve Loom's own
coordination problems, not a general-purpose external-mutex library):
- `heartbeat_claim` (`loom-daemon/src/activity/claims.rs`, wired into the IPC
  layer at `loom-daemon/src/ipc.rs:2712-2741`) keeps the `issue_claims` table's
  `last_heartbeat` column current for a live claim holder; `claim_issue` in the
  same file breaks a claim as stale only when `age_secs > stale_threshold` at
  the moment of the **next acquire attempt** — the "on next acquire, not
  scheduled" half of the pattern above, which is exactly why a resource nobody
  else ever tries to (re-)claim can stay stale-held indefinitely under this
  model alone.
- `PeerClaimView` (`loom-daemon/src/peer_claims.rs`) is the scheduled-reaper
  half: it expires a peer's soft claim once local receipt time exceeds a fixed
  TTL since the *last* observed heartbeat ad (`is_claimed_at` /
  `claimed_issues_at`), and a periodic re-advertisement
  (`sweep_registry::readvertise_peer_claims`) refreshes that clock well inside
  the TTL for a still-live holder — see the module doc comment's "TTL is
  measured against LOCAL receipt, never the advertiser's clock" section for why
  wall-clock skew across hosts rules out comparing timestamps directly.

**Prior art establishing this as Loom's actual mitigation pattern** (both
resolved without a kill-time hook, because none is available):
- #3683 — a role subagent killed mid-phase by an account rate limit left
  lifecycle steps dangling; the fix made the *lifecycle state* self-healing
  (resumable from a checkpoint) rather than depending on the killed subagent's
  own cleanup running.
- #4348 — a detached sweep killed by an external `SIGKILL` was never recovered;
  the fix was reaper-based detection of a dead PID against the sweep registry,
  not a termination signal from the kill itself.

**Out of scope**: implementing a `SubagentStop`-style hook, or any daemon-side
termination-signal channel for Task-tool subagents, is explicitly out of scope
for this section — see "Root cause" above for why.

### A dead lock-holder PID is not evidence the requesting agent is dead (#6765)

**The section above establishes one direction: a killed subagent leaves no
teardown signal, so any lock it held keeps looking "held" in its holder
record.** This section covers the mirror mistake, made by the *orchestrator*
rather than the lock's own reaper: even once a lock's recorded holder PID is
confirmed dead, that dead PID is **not** evidence that the agent which
requested the lock has exited. The two are separate facts, and treating the
first as proof of the second is unsafe in the direction that matters —
it licenses acting on someone else's still-in-progress work.

**Why the two facts are separate.** A lock's `holder.json`-style record
captures whichever PID happened to make the acquiring write — not necessarily
the dispatched agent's own PID. It can be a short-lived child process the
agent spawned to do the acquiring, or a PID left over from an earlier dispatch
the agent has since restarted past (e.g. a session-limit kill followed by a
fresh dispatch on the same issue). `kill -0 <holder-pid>` only answers "is
that specific process still running?" — a narrower question than "is the
agent that asked for this lock still working?" — and an ESRCH from `kill -0`
is exactly what a genuinely orphaned lock also looks like from the outside;
the lock record alone cannot distinguish the two.

**Incident.** A research subagent's first dispatch was killed by a session
limit; a later dispatch acquired a resource lock and wrote a fresh
`holder.json` carrying its own PID. An orchestrator status check found the
lock's recorded PID dead, read that as "the agent is dead," reaped the lock,
and then went further than reaping: it treated the agent's branch as
abandoned and ran a full review-and-merge cycle against it — opened a PR,
reviewed it, applied corrections, merged it. The agent was still alive for
roughly three more hours and discovered the collision only at its own
close-out, after `main` had already absorbed a competing review pass. Nothing
was lost only because that agent happened to re-check its base before
finishing; a less careful one would have force-pushed over the merged review
cycle or opened a competing PR from a stale branch.

**Reaping a lock and adopting the associated work are separable decisions —
keep them separate.** Reaping an orphaned lock on a dead-PID signal alone is
low-risk: the failure mode is "the resource was briefly unavailable while it
didn't need to be," which self-heals. Adopting the branch or worktree the
lock was protecting — opening a PR against it, reviewing it, merging it, or
otherwise acting as though the agent that produced it is finished — is not
low-risk, and a dead lock-holder PID does **not** license it on its own.
Do not let one decision imply the other:

- **Reap the lock** on the existing dead-PID basis alone — unchanged by this
  section.
- **Do not touch the associated branch/worktree** on that same basis. Before
  acting on it, get a **positive "this agent is done" signal**, not an
  inferred one:
  - If the agent was dispatched as an in-session Task-tool subagent within
    the *same* orchestrator session, query the harness's own task-liveness
    surface instead of inferring from a proxy — the same non-blocking poll
    (e.g. `TaskOutput` on the original dispatch's task id) documented in
    `defaults/.claude/commands/loom/sweep.md` → "Task-liveness check before
    Builder re-dispatch (#5897)". That rule was written for one call site
    (re-dispatching a Builder after a worktree-diff proxy looked like death)
    but the underlying principle — query positive liveness, don't infer it
    from a proxy — applies at any point an orchestrator is about to act on
    the assumption a dispatched subagent has died, lock reaping included.
  - If the agent was dispatched out-of-session (a separate `loom-daemon`
    sweep, a different host, any process this orchestrator cannot poll),
    there is genuinely no definitive liveness signal available from here.
    Say so explicitly rather than reaching for a proxy, and fall back to
    checking the forge-visible state of the work itself (a pushed branch, an
    open PR, a recent commit) to see whether it has already reached a
    stable, reviewable state on its own terms — that is evidence about the
    *work*, not a proxy for the *agent's* liveness, and should be read as
    such.
- **If no definitive signal is available either way, the conservative
  default is: assume live, do not adopt the branch.** Wait, or leave the
  branch for the original agent to reconcile at its own close-out (as
  happened in the incident above), rather than risk the two-writers-on-one-branch
  race a wrong adoption creates. This mirrors #5897's "prefer a bounded wait
  over immediate re-dispatch absent positive evidence of death."

Related: #6320 covers the adjacent case where a **daemon** reclaims an
in-session sweep's claim because the claim itself carries no lease record —
the same "no authoritative liveness record for in-session work" gap,
approached from the claim-record side rather than the lock side.

## Sweep Dispatch Troubleshooting

Multi-issue dispatch is driven by the Rust `loom-daemon` binary via `mcp__loom__dispatch_sweep`. The daemon holds the sweep registry, event bus, and reaper in memory — there is no on-disk orchestration state file to inspect. (The v0.9.x `spawn-loop.sh` and its `.loom/spawn-loop-state.json` state file were removed in v0.11.0.)

### Sweep MCP tools missing (stale dist bundle)

**Symptom**: `mcp__loom__dispatch_sweep`, `mcp__loom__list_sweeps`, `mcp__loom__get_sweep_status`, `mcp__loom__tail_sweep_log`, `mcp__loom__cancel_sweep`, `mcp__loom__publish_event`, `mcp__loom__subscribe_to_events`, or `mcp__loom__tail_event_bus` are **not offered** in a live session — `/loom:sweep`'s Stage -1 daemon probe can't reach them even though `loom-daemon` is running.

**Cause**: the MCP client loads the **built bundle** `mcp-loom/dist/index.js`, never the TypeScript source. `dist/` is gitignored, so a checkout that predates the sweep tools (Phase A #3452 / Phase C #3455) keeps serving an old bundle. The source (`mcp-loom/src/index.ts` → `sweepTools`) is correct; the on-disk artifact is stale (#3803).

**Diagnose**:

```bash
# 0 means the sweep tools are absent from the built bundle -> stale
grep -c dispatch_sweep mcp-loom/dist/index.js

# Compare build vs source timestamps
ls -la mcp-loom/dist/index.js
find mcp-loom/src -type f -newer mcp-loom/dist/index.js   # any output => dist is stale
```

**Fix** — rebuild, then **reconnect**:

```bash
cd mcp-loom && npm install && npm run build
grep -c dispatch_sweep dist/index.js   # should now be > 0
```

`scripts/setup-mcp.sh` now auto-rebuilds when `dist/index.js` is missing **or** older than any file under `mcp-loom/src/` (#3803), so `./scripts/setup-mcp.sh` is the safe one-shot path. Rebuilding the bundle does **not** refresh an already-running session — an MCP client caches its tool list at connect time, so you must **restart the Claude Code session** (or respawn the `loom` MCP subprocess) for the new tools to appear. See [`mcp-loom/README.md`](https://github.com/rjwalters/loom/blob/main/mcp-loom/README.md#rebuilding-after-source-changes-reconnect-required) (upstream Loom repo — not shipped to consumer installs) for the full rebuild + reconnect procedure and a raw `tools/list` verification snippet.

### MCP tools hang with no response (~1800s), then abort

**Symptom**: `mcp__loom__dispatch_sweep`, `mcp__loom__get_sweep_status`, `mcp__loom__list_sweeps`, or `mcp__loom__cancel_sweep` return **no response and no progress**, and are eventually aborted by the client (`sent no response or progress for 1800s; aborting`) — even though the underlying operation **succeeded** (the sweep child spawned, the PR opened, etc.). The CLI path (`loom-daemon status` / `dispatch`) stays fast throughout, which isolates the fault to the MCP/IPC response path, not a wedged daemon.

**Cause** (#4043): the MCP bridge's unary request transport (`mcp-loom/src/shared/daemon.ts` `sendDaemonRequest`) historically settled its promise only in the socket's `end` handler. The real `loom-daemon` holds each connection **open by design** (a persistent per-connection read loop, `loom-daemon/src/ipc.rs` `handle_client`): it writes one newline-delimited JSON response frame per request and never closes after answering. So the response sat complete-but-unparsed in the client buffer while the promise waited forever for an `end` that never came. A **stale bundle** (`mcp-loom/dist/` older than `mcp-loom/src/`) compounds it by discarding the per-call timeout, turning a diagnosable failure into the full ~1800s idle hang.

**Diagnose** — probe the raw socket (bypassing the MCP layer) and check the bundle for the timeout string:

```bash
# Does the bundle even carry the bounded-timeout fix? 0 => stale, pre-timeout bundle
grep -c "did not respond within" mcp-loom/dist/index.js

# Is dist/ older than src/? (any output => stale, rebuild needed)
find mcp-loom/src -type f -newer mcp-loom/dist/index.js
```

**Fix** — the transport now settles on the **first newline-delimited response frame** (in the `data` handler) and closes the socket after settling, so it no longer depends on the daemon closing the connection. If you are on a pre-fix bundle, rebuild and reconnect:

```bash
cd mcp-loom && npm install && npm run build   # then restart the Claude Code session
```

The `claude-wrapper.sh` MCP pre-flight (`check_mcp_server`) now rebuilds a stale bundle before the smoke test, so a fresh session on an up-to-date checkout self-heals; the regression is guarded by `mcp-loom/scripts/verify-daemon-timeout.mjs`'s respond-without-close stub case (`npm run verify:daemon-timeout`).

### Inspect running sweeps

```bash
# List all running sweeps in the daemon registry
mcp__loom__list_sweeps

# Inspect a specific sweep's state
mcp__loom__get_sweep_status --sweep_id <id>

# Tail a per-sweep log
mcp__loom__tail_sweep_log --sweep_id <id>
# (per-sweep logs also live at .loom/logs/sweep-issue-<N>.log)
```

### Cancel a sweep

```bash
# From a Claude session with the MCP server attached:
mcp__loom__cancel_sweep --sweep_id <id>

# From a shell — including over ssh to a fleet worker (#4980). Same IPC
# request, same daemon-side termination path:
loom-daemon cancel <sweep-id>
loom-daemon cancel --issue 123          # resolves the live sweep for that issue
loom-daemon cancel --issue 123 --grace 5
```

Both send SIGTERM to the sweep's **process group**, wait the grace window, then
SIGKILL — so the wrapper, the `claude` agent, and every descendant (build tools,
simulations, watcher loops) die together.

**Never hand-`kill` a sweep's pids instead.** The registry tracks the
`claude-wrapper.sh` pid; killing it leaves the underlying agent alive. On
2026-08-03 that surviving agent noticed its subprocesses had died and
*relaunched* them, against an issue whose claim the crash path had already
returned to `loom:issue` — a zombie agent the registry reported as
`in_flight: 0`. If you have already done this, `loom-daemon status` will not show
the survivors: find them with `pstree -p <pid>` / `ps -eo pid,pgid,args` and kill
the whole group (`kill -TERM -<pgid>`).

The daemon's reaper task detects dead PIDs (every 30s) and removes them from the registry, emitting `sweep.issue.*.exited` / `sweep.issue.*.crashed` events. Since #4980 it also reaps a dead leader's *surviving* process group on that same tick, so an orphaned agent no longer keeps running unclaimed work.

### Stopping the daemon does not stop the fleet — use `loom-daemon-quiesce.sh` to drain a host (#6129)

`loom-daemon-stop.sh` (and a bare `systemctl --user stop loom-daemon` /
`launchctl bootout`) stops **dispatch only** — in-flight sweep children and
scheduled role-agent ticks (Champion/Curator/Judge/Doctor/Guide) survive by
design, so stopping the dispatcher never destroys work in flight. On a Linux
`systemd --user` host they can also be **architecturally detached** from the
daemon's own process tree: `spawn-claude.sh`'s CPU-quota mechanism (#5111,
default-on) wraps each spawn in `systemd-run --user --scope`, a transient
scope parented to the user manager, not to `loom-daemon` — so it keeps running
and drawing on the token pool with no forge-visible owner even after the
daemon reports a clean stop (the 2026-08-13 `loom-worker-2` incident: role
agents kept running after `systemctl --user stop loom-daemon` reported
success).

If you are draining a host — for maintenance, cost, or an exhausted token
pool — and actually need every Loom-spawned process gone, run:

```bash
./.loom/scripts/cli/loom-daemon-quiesce.sh              # stop dispatch AND every in-flight role/sweep child
./.loom/scripts/cli/loom-daemon-quiesce.sh --dry-run     # preview every target first
```

This works the same way on launchd and systemd, and is the only mechanism
that reaches a `systemd-run --user --scope`-wrapped agent (enumerated by its
predictable `loom-agent-*.scope` name, grouped under `loom-agents.slice`) or a
launchd-reparented one (matched by `claude`/`claude-wrapper.sh -p /loom:*` on
the process table, the same shape as this section's own `pstree`/`ps` recipe
above). See [`daemon-reference.md` → "Fleet quiesce"](daemon-reference.md#fleet-quiesce--stopping-the-daemon-is-not-a-fleet-stop-6129)
for the full mechanism and the `SuccessExitStatus=`/`failed`-vs-`inactive` fix
that shipped alongside it.

### Stuck sweep child

A sweep child whose pid is alive but whose `.loom/sweep-checkpoint/issue-<N>.json` mtime is stale is likely stuck. To recover:

```bash
# Check checkpoint mtime
ls -la .loom/sweep-checkpoint/issue-123.json

# Look at the child's log for errors
tail -200 .loom/logs/sweep-issue-123.log

# Cancel it through the daemon (MCP tool, or `loom-daemon cancel <id>` from a
# shell / over ssh):
mcp__loom__cancel_sweep --sweep_id <id>

# The checkpoint survives cancellation, so re-dispatching the issue resumes
# from its last completed phase:
mcp__loom__dispatch_sweep --issue 123
```

### Dispatch is not producing sweeps

Issues need the `loom:issue` label (human-approved, ready for work) to be eligible for dispatch. If a dispatch isn't producing a sweep, check:

```bash
# 1. Confirm there are ready issues
gh issue list --label "loom:issue" --state open

# 2. Confirm the daemon is reachable and running
mcp__loom__list_sweeps

# 3. Confirm the multi-account token pool is bootstrapped (dispatch requires it)
ls -la .loom/tokens/

# 4. Look at recent sweep activity on the event bus
mcp__loom__tail_event_bus
```

Note: by default the daemon does not poll the forge for `loom:issue` items — dispatch is operator-driven via `mcp__loom__dispatch_sweep`. To dispatch a ready issue, call `mcp__loom__dispatch_sweep --issue <N>` explicitly. (The opt-in autonomous work finder (#3810, `LOOM_WORK_FINDER` / `autonomous.workFinder`, default-off) *does* poll and auto-dispatch open `loom:issue` items when enabled — see [daemon-reference.md](daemon-reference.md#autonomous-work-finder-3810).)

### Work generation (Architect / Hermit) not running

**This is by design post-v0.10.0.** The daemon does not generate work — Architect and Hermit cadence is tracked under follow-up #3381. If you need new work generated automatically, run Architect/Hermit on a cron via the Phase 2a GitHub Actions pattern (`.github/workflows/loom-*.yml`); the existing five shipped workflows cover Champion / Curator / Judge / Auditor / Guide, but Architect and Hermit cron workflows are not yet shipped.

For now, trigger them manually when the queue is empty:

```bash
claude -p "/loom:architect" --dangerously-skip-permissions
claude -p "/loom:hermit"    --dangerously-skip-permissions
```

## Overnight / long-running orchestration

> **Supervising a long window: `/loom:watch` (#4762).** The tick loop that probes
> fleet health, applies a closed set of remediations, and prints an end-of-window
> summary is a skill: `/loom:watch [--until 07:00] [--interval 25m]`
> (`--dry-run` for a single read-only tick). It assumes — and does not restate —
> the host-sleep and `.loom/` resync procedures in this section. See
> `.claude/commands/loom/watch.md`. Note the host-sleep check above does **not**
> cover *session* suspension stalling a mode-A `ScheduleWakeup` loop — see
> `watch.md` → Loop mechanics → A for that hazard and its preflight (#4930).

### Keeping the host awake (#3350)

`/loom:sweep` automatically runs `./.loom/scripts/check-host-sleep.sh` at startup
and warns when the host can sleep. This is **advisory only** — Loom never blocks
on it. Heed the warning before walking away from a long run.

- **macOS:** user-idle sleep assertions (Amphetamine, `caffeinate -dimsu`, etc.)
  do **not** reliably defeat Maintenance Sleep on Apple Silicon. Use `sudo pmset
  -c sleep 0` for AC-only sleep disable, or flip your sleep manager's "allow
  system sleep when display is off" toggle to OFF. Restore with `sudo pmset -c
  sleep 1` afterwards.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep
  --who=loom --why=loom -- <cmd>`.

Manual invocation:

```bash
./.loom/scripts/check-host-sleep.sh         # full warning (or success line)
./.loom/scripts/check-host-sleep.sh --quiet # stderr warning only, no stdout line
```

#### Making it persistent instead of advisory (`host.preventSleep`, #6311)

The check above only warns — it never mutates anything, so re-applying the
`systemd-inhibit` mitigation by hand on every run/host gets old fast. Opt a
repo IN to Loom applying it automatically via `.loom/config.json`:

```json
{ "host": { "preventSleep": true } }
```

Env override: `LOOM_HOST_PREVENT_SLEEP=1` (or `0` to force-disable).
Precedence is the standard env > config > default-OFF tier every Loom knob
uses (see `defaults/scripts/lib/host-sleep-config.sh`). An absent block, or
any value that isn't a recognizable true/false spelling, resolves to
disabled — this knob can never block or fail a sweep.

- **Linux/systemd — the actual closable gap.** With the flag on, two
  self-wrap points apply `systemd-inhibit --what=idle:sleep --who=loom
  --why=<role>` (unprivileged, no `sudo`) automatically:
  - `.loom/scripts/spawn-claude.sh` — the single dispatch chokepoint for
    BOTH headless `/loom:sweep` and scheduled role-runner spawns. `--why`
    is the child's `$LOOM_ROLE` (e.g. `sweep-lifecycle`). Verify with
    `systemd-inhibit --list` while a sweep is running — an active `loom`
    lock should be visible for its whole lifetime.
  - `loom-daemon-start.sh --foreground` — wraps the foreground daemon
    process itself. The systemd-unit-managed and nohup-fallback daemon
    launch paths are deliberately **not** wrapped (both persist the launched
    process's pid into places `loom-daemon-stop.sh` / the watchdog / `loom-daemon
    status` treat as the daemon's own identity; prefixing either with
    `systemd-inhibit` would change what that pid actually IS). In practice
    this is not a live gap: `idle:sleep` locks are host-wide, not scoped to
    one process's children, so any one active sweep/role-runner spawn keeps
    the whole host — daemon included — awake for as long as it runs.
  - Every wrap point probes first (`systemd-inhibit ... -- true`) and
    silently skips the wrap on failure (no reachable `systemd-logind`,
    `systemd-inhibit` missing, non-systemd Linux) — advisory-only
    `check-host-sleep.sh` still fires normally in that case.
  - A manually-started **interactive** session (MOM, a terminal running
    `claude` directly) is not covered by either self-wrap — wrap it by hand
    as `check-host-sleep.sh` itself still recommends.
- **macOS — never automated.** The reliable mitigation
  (`sudo pmset -c sleep 0`) is privileged and host-global; `host.preventSleep`
  is a deliberate no-op here and **never** invokes `sudo`. Once you've
  evaluated and applied a mitigation yourself, record it so the warning stops
  being permanent noise:

  ```json
  { "host": { "sleepMitigationAcknowledged": "pmset sleep=0 set at image build" } }
  ```

  (env override `LOOM_HOST_SLEEP_MITIGATION_ACKNOWLEDGED`). This downgrades
  `check-host-sleep.sh`'s full banner to a one-liner naming your mitigation —
  it never claims the host IS protected (macOS user-idle sleep assertions are
  not reliable, per the incident above), it only stops re-printing an
  already-evaluated warning on every run.

### The base-branch trap: a session-start git snapshot is not evidence about the present (#6718)

**Every agent session's context includes a git status block captured once, at
session start, and explicitly labelled as a point-in-time snapshot — it does
not update as the session runs.** This is the harness's own environment
context, not something any Loom role prompt injects or can suppress. Over a
long sweep — which by construction runs for hours and merges many PRs — that
block drifts arbitrarily far from reality: every merge the sweep performs
moves the default branch out from under it. An agent primed by role guidance
to watch for **the base-branch trap** (local default branch diverged from
`origin`, starving new worktrees of recently-merged work — see "Main Branch
Freshness (#3770)" in `sweep.md`) but holding only that stale snapshot has no
reason to distrust it: it reports a confident, plausible, and **wrong**
divergence. This happened twice independently in one session (#6718) — both
reports cited the exact commit SHA present at the parent session's start,
hours after several merges had already moved `origin`'s default branch past
it, with local, remote-tracking, and the forge's own view all agreeing on the
newer SHA once actually checked.

The false positive is more expensive than a missed one: an agent that believes
the base branch is starved reasonably refuses to dispatch, escalates to the
operator, or attempts a corrective push — all three are wrong against a branch
that is already current, and each has to be disproved by hand before the sweep
can continue.

**Any role guidance that mentions the base-branch trap (or any other
long-running check of "is my repository fact still current") must say two
things, not one:**

1. **Branch state must be read with a live command at the moment of the
   check** — never inferred from a `git status`/`git log` block already
   sitting in context, however recent it looks. A block captured at session
   start is a snapshot of that moment, not a fact about now, and nothing
   refreshes it for the rest of the session.
2. **The check must compare three refs, not one, and all three must agree**
   before concluding the branch is current:
   - the **local branch** (`git rev-parse <branch>` / `git log -1 <branch>`,
     run now, not recalled),
   - the **remote-tracking ref, after an explicit `git fetch`** (a
     remote-tracking ref you didn't just fetch is a second stale snapshot,
     not a live fact), and
   - **the forge's own view** — `gh api repos/<owner>/<repo>/branches/<branch>`
     or `gh repo view --json defaultBranchRef` for default-branch freshness,
     `gh pr view`/`gh pr list` for a PR branch — the one source not subject to
     local clock or fetch skew.

   `./.loom/scripts/check-main-freshness.sh` already performs exactly this
   bounded fetch-and-compare for the sweep-preflight instance of this check
   (see "Main Branch Freshness (#3770)" in `sweep.md`) — reach for it, or its
   pattern, rather than hand-rolling a weaker one-ref comparison. It is
   **pre-wave-only**: it runs once before the first wave/dispatch and does not
   re-fire for a Builder/Judge/Doctor subagent spawned mid-sweep, potentially
   hours and many merges later — that subagent must run its own live check
   before treating anything about branch freshness as established, not trust
   the pre-wave result or its own session-start context.
3. **A reported divergence must carry the live command output that
   established it** — paste the actual `git fetch` + ref comparison (or
   `check-main-freshness.sh`'s own output) into the report, not just the
   conclusion. This lets a downstream orchestrator or human tell a
   freshly-derived finding apart from one recalled from stale context, without
   re-deriving it themselves.

This generalizes beyond branch state: any long-running role that reasons about
a repository fact captured once at session start (a worktree's HEAD, a PR's
mergeability, a label set) is exposed to the identical failure — branch
divergence is simply where Loom's own guidance happens to prime agents to look
hardest. Because the stale snapshot is harness-injected context rather than
something a role `.md` file adds, no role brief can opt out of *receiving* it;
the only lever available at the role-prompt layer is strengthening the
guidance above so agents distrust it by default, which is why it is written
here once and pointed to, rather than restated per role.

### Keeping installed `.loom/` copies fresh after a pull (#3770 detect → #3777/#4239 resync)

The installed Loom surfaces the harness actually executes/reads are synced from
`defaults/` **at install time**. A `git pull` that merges a fix updates `defaults/`
but **not** the installed copies — so a session can run stale hooks/scripts/roles/
docs/commands indefinitely (the incident: a merged `guard-destructive.sh` fix kept
prompting until hand-copied). Before #4239 the only full-surface update path was a
destructive `install.sh --confirm-reinstall`.

This is a **detect → fix** pair:

- **Detect (#3770)** — `/loom:sweep` runs `./.loom/scripts/check-main-freshness.sh`
  at startup. When local `main` is behind `origin/main` it prints a non-blocking
  warning and flags any installed file that differs from its `defaults/`
  counterpart. Advisory only; it never pulls, merges, or resets.
- **Fix (#3777, widened #4239)** — `./.loom/scripts/resync-installed.sh` refreshes
  the installed pure-copy surfaces from the recorded Loom source (note the
  asymmetric source→target mapping):

  | Installed surface | Source under `defaults/` |
  |-------------------|--------------------------|
  | `.loom/hooks/` | `defaults/hooks/` (top-level `*.sh`) |
  | `.loom/scripts/` | `defaults/scripts/` |
  | `.loom/roles/` | `defaults/roles/` |
  | `.loom/docs/` | `defaults/docs/` |
  | `.loom/bin/` | `defaults/.loom/bin/` (live consumer CLI) |
  | `.claude/commands/loom/` | `defaults/.claude/commands/loom/` |

  It is idempotent (a no-op when in sync), reports per-file
  `updated`/`created`/`unchanged`/`skipped`, only ever touches files that exist in
  the source (repo-specific files with no source counterpart — e.g. custom roles —
  are left alone), and **never clobbers a symlinked install target** (in this
  dogfood repo the `.loom/docs/*.md` entries are symlinks back into `defaults/`;
  those are reported `skipped`, never overwritten). Loom-internal files declared in
  `defaults/.loom-internal.list` are skipped (never resurrected into a consumer).
  On a successful non-dry-run it also re-stamps `loom_version`, `loom_commit`, and
  a new `last_resync` date into `.loom/install-metadata.json`. Since that file is a
  machine-local stamp every host's resync rewrites, resync also ensures (every
  run, self-healing existing installs) a `merge=ours` attribute for it in a
  Loom-managed `.gitattributes` block plus the required local (never committed)
  `git config merge.ours.driver true` — so two hosts that each committed a resync
  no longer conflict on `git merge`/`git pull`; the file is fully re-derived by the
  next resync regardless of which side "wins" (#4528). One guard exception
  (#4041): the vendored generic guard `hooks/guard-destructive-generic.sh` is
  **not** resynced (and any stale copy is removed) in a repo where the canonical
  Repo Skills guard (`.claude/skills/repo/hooks/guard-destructive.sh`, carrying the
  rjwalters/repo#29 fix) is installed — the `guard-destructive.sh` dispatcher
  defers to the canonical guard there.

**Out of scope** (resync never touches these — they update by other mechanisms):
`.loom/config.json` (operator-owned; needs merge-semantics design), `CLAUDE.md`
(repo-customized; needs managed-section markers), `.github/labels.yml` +
`.github/workflows/*` (covered by `gh label sync` + install-time opt-ins), the
`loom-daemon` binary (#4055 self-update), `.mcp.json` (regenerated by
`scripts/setup-mcp.sh`), and the metadata `install_date` + `installed_files`
fields (installer-owned).

**Run it from the main checkout only (#4563).** The installed `.loom/` is always
resolved against the **primary** worktree (via `git rev-parse --git-common-dir`),
so a resync launched from a linked worktree — an issue/PR worktree under
`.loom/worktrees/` — writes to the **main checkout**, not to the worktree you are
standing in. That is exactly how a wave-2 Builder contaminated `main` mid-sweep on
2026-07-30 (four installed paths written into `main` and quarantined by
`check-main-clean.sh`). The script therefore **refuses to run** (exit `1`,
including under `--dry-run`) when its own `git rev-parse --show-toplevel` differs
from the resolved main-checkout root. Landing a `defaults/` change does **not**
require you to resync: installed-copy propagation is the periodic
`chore: resync installed Loom surfaces` commit's job, made from the main checkout
after the change merges. An operator who really does mean "rewrite the main
checkout's installed copies from this worktree" can pass `--allow-worktree` (or
export `LOOM_RESYNC_ALLOW_WORKTREE=1`); it then proceeds with a warning naming the
main-checkout target. Running from the main checkout — including any subdirectory
of it — is unaffected.

**Generating a complete resync while the fleet is live (`--output`, #6106).**
`--allow-worktree` and a bare re-run from the main checkout are both unsafe
whenever the daemon may be actively dispatching sweeps in that same checkout —
which on a fleet host is most of the time — because they write dozens of files
directly into a checkout something else might be reading or writing concurrently.
`--output <dir>` (or `LOOM_RESYNC_OUTPUT=<dir>`) is the safe alternative: it
creates a disposable, **detached** `git worktree` at `<dir>` (via `git worktree
add --detach <dir> HEAD` against the primary checkout — registering only new
`.git/worktrees/` metadata, never reading or writing a single file in the primary
checkout's own working tree) and resyncs **into that staging worktree** instead of
the primary. Because nothing is written to the primary checkout either way, the
`#4563` linked-worktree refusal does not apply when `--output` is given — it can
be run from anywhere (the main checkout or any linked worktree) at any time,
including mid-sweep, with zero risk to the live checkout:

```bash
./.loom/scripts/resync-installed.sh --output /tmp/loom-resync-staging
cd /tmp/loom-resync-staging
git checkout -b chore/resync-installed-$(date +%Y%m%d)
git add -A && git commit -m 'chore: resync installed Loom surfaces'
git push -u origin HEAD   # open a PR from here
cd - && git worktree remove /tmp/loom-resync-staging   # from the primary checkout when done
```

The staging worktree is a real, independent git checkout at the primary's current
`HEAD` — not a bare file copy — so once the sync completes it is immediately a
normal place to `git add`/`commit`/`push` from. `--dry-run` combined with
`--output` still creates the staging worktree (it is the preview's target) but
auto-removes it before exiting, so a preview leaves no residue either way. The
refusal message itself now names `--output` as the safe path, ahead of
`--allow-worktree`.

**When several `defaults/` PRs merge between periodic resync runs.** The periodic
`chore: resync installed Loom surfaces` commit only fixes drift that existed *at
the time it ran* — if N more `defaults/`-touching PRs merge after that commit (a
common pattern in a busy fleet session, e.g. six PRs merging back-to-back on
2026-08-12 before the next periodic resync landed), the installed copies fall
behind again immediately, and an already-open resync PR that was branched before
some of those N PRs merged can close only part of the gap once rebased. There is
no separate tracking mechanism for this — the existing tools already cover it, but
only if you re-run them **after the last relevant merge**, not once at the start
of a merge wave:
- `./.loom/scripts/check-main-freshness.sh` (or `resync-installed.sh --dry-run`,
  exit `2` on drift) tells you whether the installed copies are stale **right
  now** — re-run it again after each additional `defaults/` merge rather than
  trusting a check from before the wave, since drift accumulates with every merge.
- A resync PR opened mid-wave is a **partial** fix by construction, not a bug in
  the PR itself. Prefer generating (or regenerating) the resync **after** the
  wave settles, via `--output` above so it can be done immediately without
  waiting for a quiet window — a single complete resync after N merges is
  simpler to review than N sequential partial ones.
- If a resync PR is already open when more `defaults/` PRs land, rebase it (or
  regenerate it with `--output`) before merging rather than merging it as-is and
  assuming the gap is closed — `--dry-run` after rebase confirms whether any
  drift remains.

**It can safely update itself (#4669).** `resync-installed.sh` is one of the files
under `defaults/scripts/`, so every run copies a newer version over the very path
the running Bash process is still reading from. It used to do that with an
in-place `cp`, which truncates and rewrites the destination: Bash then resumed
reading its own (now shorter) script at a stale byte offset and either died with
`syntax error near unexpected token` or fell off the end mid-run — leaving dozens
of surfaces refreshed, the rest stale, and no summary saying so. Now every file
is staged beside its destination and `rename(2)`-d into place (atomic; the
already-open inode is left intact), and the self-copy is additionally **deferred
until every other surface has settled**. If a file cannot be staged or renamed
(read-only mount, permissions, no disk space) the run still finishes the
remaining files and then prints an explicit **`PARTIAL REFRESH`** block naming
every failed path and **exits `1`** — nothing is ever half-written, so fixing the
cause and re-running completes the refresh.

The intended flow is **"freshness warning says you're stale → run resync"**:

```bash
cd <main checkout>                              # NOT .loom/worktrees/issue-N (#4563)
git merge --ff-only origin/main                 # bring defaults/ current
./.loom/scripts/resync-installed.sh --dry-run   # preview what would change (exits 2 on drift)
./.loom/scripts/resync-installed.sh             # apply
```

`--dry-run` makes no changes and exits `2` when drift is detected (so it doubles
as a check). To pin an intentional per-repo customization so resync never
overwrites it, list its relative path (e.g. `hooks/guard-destructive.sh`,
`roles/custom-role.md`, `bin/loom`, or `commands/loom/mine.md`) — one per line — in
`.loom/resync-ignore`; matching files are reported `skipped`. A full `loom-daemon
init` / installer run already performs the equivalent recursive copy, so a normal
reinstall keeps the copies current too.

**Precondition: this flow needs a resolvable `defaults/` source tree (#6202).**
`resync-installed.sh` resolves its source in priority order: (1) this checkout
IS the Loom source repo (`defaults/hooks` or `defaults/scripts` present), (2)
the gitignored `.loom/loom-source-path` sidecar (written only by a local
`install.sh` / `install-loom.sh` run) points at a local clone of it, or (3) a
legacy `install-metadata.json` `"loom_source"` field (dead for any post-#5624
install — that field is no longer written, since it leaked the installing
machine's absolute path). **None of these exist on a checkout that never ran
the Loom installer locally** — a fresh developer clone, a CI checkout, or any
machine that received the repo rather than installing into it — which is
exactly the population most likely to be running stale surfaces, since they
never ran the installer that would have refreshed them. On that population the
script fails on first use with `Could not locate a defaults/ source tree to
sync from`. `check-main-freshness.sh` now detects the same gap and appends a
note to its own staleness warning before you reach that failure, rather than
only after (#6202). Fix: clone <https://github.com/rjwalters/loom> locally,
then either re-run its installer against this repo or write the sidecar
yourself: `echo /path/to/local/loom-clone > .loom/loom-source-path`.

**`.loom/loom-source-path` is a DURABLE pointer — never point it into scratch
space (#6780).** Every future `resync-installed.sh` / `check-main-freshness.sh`
run keeps reading whatever path is recorded here, for as long as this repo is
installed, not just for the session that wrote it. Installing from a clone
made inside a temporary/scratch directory (`/tmp/...`, `/private/tmp/...`, an
agent session's scratchpad, a CI job's workdir) is fine as a one-off, but
recording that transient location as the repo's permanent upstream pointer
turns it into a dead reference the moment the scratch area is cleaned up —
`install.sh` and `scripts/install-loom.sh` now print a non-blocking warning at
install time when the resolved source path looks ephemeral, so re-point the
sidecar at a persistent clone if you see it:
`echo /path/to/persistent/loom-clone > .loom/loom-source-path`. Both
`resolve_defaults()` (`resync-installed.sh`) and its read-only mirror
`resync_precondition_met()` (`check-main-freshness.sh`) also require the
resolved path to actually contain a populated `defaults/hooks` or
`defaults/scripts` — a sidecar pointing at a directory that still exists but
is stale/empty (rather than fully vanished) is treated as unresolvable too,
not silently reported as "in sync" against nothing.

The same list also declares a file **repo-owned**, so the installer's reinstall
clean sweep never deletes it — see
[`repo-owned-files.md`](repo-owned-files.md) for the full ownership rule that
governs files living inside `.loom/hooks/` and the other managed directories.
