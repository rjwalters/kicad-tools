# Sweep — Stop conditions, pre-wave advisory checks, working-set contract, coexistence

> **Reference file for [`sweep.md`](sweep.md)**, the `/loom:sweep` dispatcher.
>
> **Load when:** **before the first wave** (or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call) for the three advisory checks, and whenever a peer `/loom:sweep` or a daemon may be sharing this repo.
>
> **Flat, one level deep.** `sweep.md` names every file a given run needs up
> front; nothing here requires opening a *third* file to follow its own
> procedure. Cross-references worded "above"/"below" that do not resolve inside
> this file point at a sibling `sweep-*.md` section — see the reference-file map
> in [`sweep.md`](sweep.md). The body below is **verbatim** from the pre-split
> `sweep.md` (#7726): no rule, warning, edge case, table, or code block was
> reworded, softened, reordered, or dropped.

## Contents

- [Stop Conditions](#stop-conditions)
- [Host Sleep Readiness (#3350)](#host-sleep-readiness-3350)
- [Main Branch Freshness (#3770)](#main-branch-freshness-3770)
- [Outstanding Quarantine Stashes (#5185)](#outstanding-quarantine-stashes-5185)
- [Sweep Child Working-Set Contract (#3980)](#sweep-child-working-set-contract-3980)
- [Coexistence (peer `/loom:sweep` and legacy daemon)](#coexistence-peer-loomsweep-and-legacy-daemon)

---

## Stop Conditions

Stop processing and print the summary when any of these conditions hold:

- The issue list is exhausted.
- The user interrupts (Ctrl-C or explicit stop).
- An unrecoverable error occurs (e.g., `gh` is not authenticated, repository state is broken). Log the error and exit.

This skill does **not** implement a disk-pressure *stop* condition (aborting an in-flight sweep when the disk fills), max-waves caps, or doctor-cycle global limits — those are deferred (see Limitations). It **does** apply a disk-headroom *gate* when resolving the auto wave size at Stage -1 (see "Resolve auto wave size"): the scratch-volume free space clamps the initial wave size down, but does not stop a running sweep.

## Host Sleep Readiness (#3350)

Long sweeps run for many minutes — sometimes hours overnight — and the host going to sleep mid-run tears down in-flight subagent sockets to `api.anthropic.com`, killing curator / builder / judge subagents and losing all their work (see #3350 for the incident report).

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** (see "The daemon-dispatch path" above) — run the host-sleep readiness check and surface its output to the user:

```bash
./.loom/scripts/check-host-sleep.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It prints a platform-aware warning to stderr when the host is configured in a way that allows it to sleep:

- **macOS:** even with a user-idle sleep assertion (Amphetamine, `caffeinate -dimsu`, etc.), macOS Maintenance Sleep can still fire and tear down sockets. The reliable defenses are `sudo pmset -c sleep 0` or flipping your sleep manager's "allow system sleep when display is off" toggle to OFF.
- **systemd Linux:** wrap the session in `systemd-inhibit --what=idle:sleep --who=loom --why=sweep -- <cmd>`, which IS reliable.

If the user is running an overnight sweep, they should heed the warning before walking away.

## Main Branch Freshness (#3770)

During a long sweep, other PRs can merge to `origin`'s default branch. Because the installed `.loom/scripts/` and `.loom/hooks/` copies are synced from `defaults/` at install time, a local default branch that has drifted behind `origin` means the session may be executing **stale orchestration scripts** that silently lack recently-merged logic. This actually happened (#3770): during a 2026-07-22 sweep, `worktree.sh --base` (#3742) and `merge-pr.sh` auto-reconcile (#3752) were absent from the copies the session was running even though both had merged to `origin/main` — a running sweep had no signal it was behind.

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** — run the main-freshness check and surface its output to the user (same timing and sibling role as the Host Sleep Readiness check above):

```bash
./.loom/scripts/check-main-freshness.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It is strictly **read-only**: it never runs `git pull` / `git merge` / `git reset` and never auto-reconciles. It does a bounded `git fetch` of the default branch (degrading gracefully to the last-known ref when offline), then compares the local default branch against `origin/<default-branch>`:

- **Behind by N commits:** prints a bordered warning to stderr noting that installed `.loom/scripts/` / `.loom/hooks/` copies may be stale, with the remediation `git merge --ff-only origin/<default-branch>`. When it can resolve both trees it also best-effort notes any installed script/hook whose content differs from its `defaults/` counterpart.
- **Up to date:** prints nothing to stderr; a one-line stdout confirmation (suppressible with `--quiet`, matching `check-host-sleep.sh`).

If the check warns, the operator should refresh local `main` (and re-sync installed copies if their install flow does so) before relying on stacked-dependency or auto-reconcile behavior mid-sweep.

**This check is pre-wave-only — it does not cover a subagent spawned mid-sweep
(#6718).** It runs exactly once, before the first wave/dispatch, using a live
`git fetch` at that moment. A Builder/Judge/Doctor child spawned later in the
same run — potentially hours and many merges afterward — never re-triggers it,
and that child's own session context still carries whatever git-status
snapshot was captured when *its* session started. If that child's guidance
leads it to reason about base-branch divergence (e.g. "are my worktrees
branching from current work?"), it must run its own live check — this
pre-wave result and the child's own session-start context are both stale by
construction, not evidence about the present. See
[`troubleshooting.md` → "The base-branch trap: a session-start git snapshot is
not evidence about the present"](../../../.loom/docs/troubleshooting.md) for
the three-ref live check every base-branch-trap mention must point to, and for
why a reported divergence must carry the live command output that established
it.

## Outstanding Quarantine Stashes (#5185)

`check-main-clean.sh --quarantine` (see the Wave Lifecycle "Backstop" step above) rescues contamination it finds in the main worktree into a labeled `git stash` entry — `On <branch>: loom-quarantine: run=<RUN_ID> issue=<N>` — rather than discarding it. This is correct and loses no data, but the quarantine is otherwise recorded only in the structured `.loom/logs/main-quarantine.log` JSON log; nothing surfaces that a rescue stash is outstanding. A labeled stash can therefore sit indefinitely with nobody aware there is quarantined work to reconcile — noticed, if at all, only by chance (e.g. an unrelated command that happens to count stashes).

**Before the first wave — or, on the daemon path, before the first `mcp__loom__dispatch_sweep` call** — run the quarantine-stash check and surface its output to the user (same timing and sibling role as the Host Sleep Readiness and Main Branch Freshness checks above):

```bash
./.loom/scripts/check-quarantine-stashes.sh
```

This is advisory-only. The script always exits `0` and **must not block** the sweep — proceed regardless of what it prints. It is strictly **read-only**: it never pops, drops, or applies a stash. `refs/stash` is shared across every worktree of the repo (not per-worktree — see the #4821 note under "CRITICAL: Only Builders parallelize"), so the check is meaningful regardless of which worktree it runs from:

- **≥1 outstanding `loom-quarantine:` stash:** prints a bordered warning to stderr listing each stash's `stash@{N}` selector, relative age, and label (run id / issue number), with `git stash show -p <ref>` (inspect), a **replay into the owning issue worktree** (`git stash show -p <ref> | git -C .loom/worktrees/issue-<N> apply -`), and `loom-daemon stashes retire --issue <N> --execute` (retire) as the reconciliation commands. It deliberately does **not** suggest `git stash pop` (#6076): a pop in the primary clone is an unanswerable `stash-scope:main-checkout` ask in a headless run, and it re-contaminates main.
- **None outstanding:** prints nothing to stderr; a one-line stdout confirmation (suppressible with `--quiet`, matching `check-host-sleep.sh` / `check-main-freshness.sh`).

If the check warns, the operator should reconcile each listed stash into the issue worktree it belongs to (or consciously drop it) — this does not block the current sweep, but stale quarantines accumulate silently otherwise.

## Sweep Child Working-Set Contract (#3980)

Every dispatched child of a sweep — Curator/Builder/Judge/Doctor/Champion subagents, and any test suite or tool subprocess they invoke — is expected to stay within a fixed filesystem working set:

- the **workspace root** it was dispatched into (the repo checkout or its issue worktree under `.loom/worktrees/issue-<N>`),
- **`.loom/`** (worktrees, logs, tokens, checkpoints),
- **`.claude*`** config directories, and
- **`$TMPDIR` / `/private/tmp`** scratch space.

This matters most on macOS running the daemon as a launchd LaunchAgent (#3972): unlike the legacy nohup model, a launchd job is its own TCC-responsible process, so any child that reaches outside this contract into a protected folder (`~/Desktop`, `~/Documents`, `~/Downloads`, `~/Pictures`, `~/Music`, `~/Library/Mobile Documents`/iCloud, …) triggers a fresh macOS permission prompt — see [`.loom/docs/daemon-reference.md` § "macOS TCC hygiene under launchd"](../../../.loom/docs/daemon-reference.md#macos-tcc-hygiene-under-launchd-3980) for the full incident, the fix already applied to `claude-wrapper.sh`'s crash-recovery path, and why Full Disk Access is never the right remediation.

Recursive scans that escape this contract — `find ~`, `du -sh ~`, `grep -r` rooted at `$HOME`, a script that `cd`'d to the wrong place before globbing, a test suite writing fixtures to `~/Documents` instead of a tmpdir, a tool resolving an iCloud-synced path — are **out-of-scope defects** in the offending role prompt, hook, or test fixture, not ambient behavior. If a sweep child needs scratch space, it should stay under the workspace root or `$TMPDIR`, never under a bare `$HOME`-relative path.

## Coexistence (peer `/loom:sweep` and legacy daemon)

`/loom:sweep` coexists with two **distinct** kinds of other runner, detected by two **separate** mechanisms. Do not conflate them: "another `/loom:sweep` is running" (peer detection, #3768) is not the same as "the legacy daemon is running" (daemon-PID check). Both warnings are **loud but non-blocking** — warn once, never auto-stop, never block.

### Peer `/loom:sweep` detection (#3768)

The primary coexistence case in the current architecture is **another live `/loom:sweep` invocation in the same repo**. This is handled at sweep start by "Sweep Run Identity + Peer-`/loom:sweep` Detection" (Step 0b, above): `sweep-run-registry.sh peers "$RUN_ID"` lists other runs whose registered liveness PID is still alive (pruning dead-PID entries so a SIGKILL'd peer never warns forever), and a loud non-blocking warning fires when any are found.

Two concurrent sweeps are now **run-state isolated**, not just label-isolated:

- **Per-issue `loom:building` claims** (step 1 pre-flight) already prevent two sweeps from building the same issue — if a peer claimed an issue first, this sweep sees `loom:building` and skips. The existing-PR probe (#3359) is the complementary defense when a PR exists but the `loom:building` label was never set / since removed.
- **Main-clean baseline** is keyed by `RUN_ID` (`main-clean-baseline-${RUN_ID}.txt`), so a peer sweep's `--snapshot` can never clobber this run's pre-sweep baseline (the #3648 contamination backstop stays correct under concurrency).
- **Checkpoints** carry this run's `RUN_ID` as `task_id`, so a sweep can tell its own `.loom/sweep-checkpoint/issue-<N>.json` writes apart from a peer's.

What remains a shared, un-isolated surface is the **default branch itself**: both sweeps merge into a moving `main`, unaware of each other's in-flight PRs. The peer warning exists so the operator knows that; isolating the merge target is out of scope for #3768 (stacking is #3759's concern).

### Legacy daemon coexistence

> **Note**: the legacy `./.loom/scripts/daemon.sh` was removed in #3432 and is not restored. The historical PID-file daemon (`.loom/daemon-loop.pid`) is not part of the current architecture; the check below is a defensive coexistence guard that fires only if such a process is somehow already running — normally a no-op. The Tier 2 dispatch backend is now the Rust `loom-daemon` binary (observed via `mcp__loom__list_sweeps`); the background agent-pool control surface is `.loom/bin/loom start|status|stop`.

`/loom:sweep` does not require the daemon and does not interact with `.loom/daemon-state.json` for writes. If a legacy daemon process is running, `/loom:sweep` and the daemon may both try to claim the same `loom:issue` label. This is a **different** mechanism from peer-`/loom:sweep` detection above — the daemon is identified by its own `.loom/daemon-loop.pid`, not by the `.loom/sweep-run/` registry.

**Coexistence behavior:** before the first wave, check whether the daemon is running. If it is, warn the user once at the start of the sweep:

```bash
PID=$(cat .loom/daemon-loop.pid 2>/dev/null)
if [[ -n "$PID" ]] && kill -0 "$PID" 2>/dev/null; then
  echo "⚠️  Loom daemon is running (PID $PID). /loom:sweep will race with the daemon"
  echo "   for issues in the loom:issue queue. Consider stopping the pool first:"
  echo "       ./.loom/bin/loom stop"
fi
```

Do not auto-stop the daemon. Do not block on this warning — proceed with the sweep. The same dead-PID liveness pattern (`kill -0`) is used by peer-`/loom:sweep` detection.

### Modern daemon coexistence (`autonomous.roleRunner` / champion-on-idle, #4884)

> **This is a different mechanism from "Legacy daemon coexistence" above — do not conflate the two.** The legacy PID-file daemon only ever raced `/loom:sweep` for `loom:issue` **label claims**. The modern Rust `loom-daemon`'s role runner is materially more disruptive: `autonomous.roleRunner.enabled=true` with `champion` in `roleRunner.roles` (interval cadence, every 5-15 min per role) or in `roleRunner.onIdle` (idle-edge cadence, #4364 — the config commonly called "champion-on-idle") periodically dispatches a live Champion subagent that **merges approved PRs and closes issues directly on the forge**, not merely claims labels. See [`.loom/docs/daemon-reference.md`](../../../.loom/docs/daemon-reference.md) for the full `roleRunner` config surface.

A `/loom:sweep` run sharing a repo with an active role-runner Champion can therefore find its own planned candidates already merged or closed by a later wave — externally completing part of the wave plan the confirmation gate committed to. This is the exact incident #4884 documents: on 2026-07-31, the daemon's role-runner Champion merged 3 PRs and closed 4 issues while a sweep's waves 1-2 were still running, completing the sweep's entire planned wave 4 and part of wave 3 before those waves ever started.

**Coexistence behavior:** `/loom:sweep` does not pause, stop, or coordinate with a role-runner Champion — same no-daemon-state-writes posture as the legacy-daemon case above. Instead, the two re-verification defenses catch the drift: per-issue pre-flight (step 1 of the Wave Lifecycle) re-reads live state for each candidate immediately before it is dispatched, and "8a. Wave-boundary candidate re-verification" (Wave Lifecycle, #4884) re-reads the **entire remaining candidate list** at every wave boundary specifically because a role-runner Champion can complete several candidates between waves, not just between pre-flight and dispatch of one issue. A candidate found already merged/closed by either check is logged and surfaced in the Summary Output as `completed externally (daemon/champion)`, distinct from a sweep-driven `merged`/`blocked`/`skipped` outcome (see "Summary Output" above). Detecting whether a role-runner Champion is active: a reachable `loom-daemon` (Stage -1's `PROBE_DAEMON`, reused if already probed this run) whose resolved `.loom/config.json` has `autonomous.roleRunner.enabled=true` with `champion` in `roleRunner.roles` or `roleRunner.onIdle`. As with the legacy-daemon and peer-`/loom:sweep` cases, this is **loud but non-blocking**: warn once (naming which mechanism was detected — legacy PID-file vs. modern role-runner), never auto-stop the daemon or Champion, never block the sweep.

