# Fleet Watch

Keep the fleet running smoothly over a long, unattended window — a **tick loop**
that probes fleet health, applies a small set of pre-authorized remediations, and
prints an end-of-window summary.

**Arguments**: `$ARGUMENTS`

```
/loom:watch [--until HH:MM] [--interval 25m] [--dry-run] [--max-ticks N]
```

This is the skill form of the manual "night watch" an operator otherwise runs by
hand: on 2026-07-30→31 that watch ran 21 ticks, re-typed the same five-check
battery every time, produced 42 merges, two `#4694` false-dead saves, and one
`#4688` pre-emptive mitigation — and burned ~60 tool calls of pure loop mechanics
getting there. Everything hard-won in that night is encoded below.

> **Naming.** Shipped as `watch` (the alternatives considered on #4762 were
> `sustain` and `night-shift`). `watch` was chosen because the loop is
> **observe-first**: the default posture every tick is *look, decide, usually do
> nothing*, and remediation is the exception. `night-shift` over-narrows it to
> overnight use (the same loop is useful for a 90-minute lunch window), and
> `sustain` implies the skill *drives* work — it does not; the daemon does. It
> also matches the verb operators already use ("watch the fleet") and the
> existing `watch_registry.rs` / watchdog vocabulary.

## What this skill is NOT

- **Not a work generator.** It never dispatches sweeps to keep itself busy. The
  daemon's work finder (or the operator) decides what gets built; the watch only
  notices when dispatch has *stopped* and repairs the cause.
- **Not a replacement for the watchdog.** `loom-daemon-watchdog.sh` (#4011) is a
  host-side, out-of-process detector that survives this session dying. The watch
  is a *supervising reader* that can reason and repair; it complements the
  watchdog, and if the two disagree the watchdog's evidence wins on liveness.
- **Not a merge bot.** Champion merges PRs. The watch does not merge, review, or
  edit code.

---

## Arguments

| Flag | Default | Meaning |
|------|---------|---------|
| `--until HH:MM` | none (runs until `--max-ticks`, or until the operator stops it) | Local wall-clock end of the window. `07:00` means "next occurrence of 07:00" — if it is already past today, it means tomorrow. |
| `--interval <dur>` | `25m` | Time between ticks. Accepts `90s`, `10m`, `1h`. Clamp to `[5m, 60m]`; a value below 5m burns context for no signal, above 60m misses outages long enough to matter. |
| `--dry-run` | off | **Exactly one tick**, report only, zero mutations. See "Dry-run mode". |
| `--max-ticks N` | unbounded (or derived from `--until`) | Hard ceiling on tick count. Always honoured, even if `--until` has not been reached. |

If neither `--until` nor `--max-ticks` is given, ask the operator for a window
before starting — an unbounded watch in an interactive session is a context leak.
In a non-interactive context, default to `--max-ticks 1` (a single tick and a
report) rather than looping; see "Loop mechanics".

Parse leniently: `/loom:watch until 7am`, `/loom:watch 'until 07:00, every 20
minutes'`, and `/loom:watch --until 07:00 --interval 20m` all mean the same
thing. Echo the resolved window/interval/tick-budget back before the first tick.

---

## Tick 0: preflight

Run once, before the loop. Cheap, read-only, and it establishes the baseline the
end-of-window summary diffs against.

1. **Resolve the health probe.** Determine whether the consolidated
   `loom-daemon health` command exists on *this* binary (see "Health probe" —
   this is a capability gate, not an issue-number gate).
2. **Resolve the managed roots.** `loom-daemon status --json` lists the
   workspaces the daemon manages; that set — not a hardcoded list — is what
   "fleet" means for the rest of the window.
3. **Record the baseline** for the summary: current time, per-repo merged-PR
   count for "today", open `loom:issue` / `loom:building` counts per root, token
   pool healthy/total, daemon pid + start time.
4. **Host sleep (advisory).** Run `./.loom/scripts/check-host-sleep.sh --quiet`.
   A host that can sleep will silently pause the whole fleet mid-window. Warn
   loudly, once; never block on it. (Details:
   [`.loom/docs/troubleshooting.md` → Overnight / long-running orchestration](../../../.loom/docs/troubleshooting.md).)
   This check covers **host** sleep only — it says nothing about whether the
   *session* driving a mode-A `ScheduleWakeup` loop will stay alive for the
   window; see the mode-A hazard note under "Loop mechanics" (#4930).
5. **Print the plan**: window, interval, tick budget, health-probe mode
   (`native` / `fallback`), managed roots, and — in `--dry-run` — the words
   `DRY RUN: no remediation will be attempted`.

---

## Health probe (`loom-daemon health`)

Each tick issues **one** consolidated probe and branches on its exit code:

```bash
loom-daemon health --since <interval> --json
```

**Exit-code contract** (from #4761): `0` healthy · `1` degraded (some check
non-green) · `2` daemon genuinely dead.

### Capability gate — detect the subcommand, never the issue number

`loom-daemon health` may not exist on the installed binary. **Gate on the
subcommand's presence**, not on whether some issue has merged:

```bash
if loom-daemon health --help >/dev/null 2>&1; then
  HEALTH_MODE=native
else
  HEALTH_MODE=fallback
fi
```

> **Trap — exit code 2 is ambiguous.** `clap` exits **2** on an *unrecognized
> subcommand*, which is the exact code the health contract assigns to "daemon
> genuinely dead". **Never interpret exit 2 as a dead daemon unless
> `HEALTH_MODE=native` was established in preflight.** If the probe exits 2 and
> its stderr matches `unrecognized subcommand` / `error: unrecognized`, that is a
> capability miss — downgrade to `fallback` mode and re-probe. Killing (or
> "restarting") a live daemon because an old binary did not know the word
> `health` is the single worst thing this loop can do.

### Fallback battery (`HEALTH_MODE=fallback`)

Until `loom-daemon health` is present, synthesize the same verdict from the
checks the manual night watch used. Same three verdicts, same downstream
branching:

| Section | Fallback probe |
|---------|----------------|
| Trusted liveness | `pgrep -f '[l]oom-daemon'` **and** the pid file (`.loom/.daemon.pid`, or `$HOME/.loom/.daemon.pid` in machine mode) — both, cross-checked |
| Dispatch state | `loom-daemon status --json` (in-flight sweeps, dynamic cap + which term binds, health-gate halt state) |
| Token pool | `loom-daemon tokens check --ranking --json` (or, to stay cheap, the mtime + contents of `.loom/tokens/.ranking`) |
| Role ticks | tail `~/.loom/daemon.log` for the window; count *persistent* failures only (a failure that self-recovered on the same root's next tick is transient — ignore it) |
| Queue depth | `gh issue list --label 'loom:issue' --state open` per managed root |
| Throughput | merged PRs in the window per root (`gh pr list --state merged --search 'merged:>=<window-start>'`) |

Derive the verdict: **dead** if liveness fails the cross-check → treat as exit 2;
**degraded** if any other section is non-green → exit 1; else **healthy** → 0.

Keep the whole battery under ~6 shell calls per tick. The point of #4761 is to
make this one call; do not let the fallback grow past the thing it replaces.

---

## The tick

Each tick, in order:

1. **Probe** (above).
2. **Classify** the verdict: healthy / degraded (with the specific non-green
   sections named) / dead.
3. **Decide** using the remediation playbook — at most **one** remediation class
   per tick unless two are provably independent (e.g. a token refresh on root A
   and a runtimes backfill on root B).
4. **Act** (skipped entirely in `--dry-run`).
5. **Record one line** into the running tick log kept in-session:
   `HH:MM tick N — <verdict> — <what changed since last tick> — <action taken or "no action">`.
6. **Sleep** to the next tick (see "Loop mechanics").

**A healthy tick produces exactly one line and no tool calls beyond the probe.**
Resist the urge to "look around" on a green tick — 21 green ticks that each cost
6 calls is how a watch runs itself out of context before dawn.

### Two-tick confirmation rule

Except for a **verified-dead daemon** (which is acted on immediately), no
remediation fires on a single tick's evidence. A condition must be present on
**two consecutive ticks** before you act on it. Most degradations self-heal
within one interval — a token that rate-limited, a role tick that lost a race, a
transient forge 5xx — and acting on the first sighting is how a watch manufactures
the outage it was supposed to prevent.

---

## Remediation playbook

Every entry: **trigger → verification → action → stop condition**. The
verification column is not optional; the whole reason this playbook exists is
that the obvious trigger has, in production, been wrong.

### R1 — Daemon appears dead

- **Trigger**: health exit 2, or the fallback liveness cross-check failing.
- **Verification (mandatory)**: `pgrep -f '[l]oom-daemon'` **and** the pid file,
  cross-checked. **NEVER act on `loom-daemon status`'s liveness line alone
  (#4694)** — its launchd domain resolver does a single `launchctl print
  gui/<uid>` probe and permanently falls back to `user/<uid>` on *any* non-success,
  so a transiently slow probe reports a live, dispatching daemon as dead. During
  the reference night this misfired twice; a daemon with **6 sweeps in flight**
  was reported dead, and only the singleton guard prevented an operator from
  killing all six. The same false negative also drives the `Protection: watchdog
  job not provisioned` line — treat that line as advisory, never as a trigger.
- **Action** (only when pgrep **and** the pid file both say dead):
  ```bash
  ./.loom/scripts/cli/loom-daemon-start.sh --from-config
  ```
  **`--from-config` is mandatory.** A bare start is FLAGS-OFF by design (#3911):
  it brings up a reliability daemon with the work finder and health gate **off**,
  which looks like a successful recovery while dispatch stays dead. The reference
  night's own start-script rerun caused exactly that downgrade.
- **Then verify the relaunch (#4232).** A clean-exit restart does not always get
  relaunched by the supervisor. Confirm a **new pid** exists after the start; if
  none appears within ~30s, `launchctl kickstart -k gui/$(id -u)/<daemon-label>`
  (macOS) or `systemctl --user start <unit>` (systemd), then re-verify. Never
  assume the restart took.
- **Stop condition**: one restart attempt per tick, at most **two per window**. A
  third death in a window is an escalation (E2), not a third restart.

### R2 — Runtime admission rejecting dispatch (#4688 shape)

- **Trigger**: dispatch attempts non-zero but admissions zero, and the daemon log
  shows `runtime admission rejected role=… runtime=claude source=default-config:
  role manifest …/defaults/roles/builder.json: No such file or directory`.
- **Verification**: for each managed root, `test -d <root>/.loom/runtimes`. The
  incident shape is a consumer repo that has `.loom/roles/` but **no**
  `.loom/runtimes/` — historically never provisioned by install or resync, so it
  cannot self-heal, and one bad tick rejected **21 of 21** dispatches fleet-wide.
- **Action**: backfill the directory per affected root — preferred
  `./.loom/scripts/resync-installed.sh` (once it provisions `runtimes`), else the
  literal mitigation: `mkdir -p <root>/.loom/runtimes && cp <loom-checkout>/defaults/runtimes/*.json <root>/.loom/runtimes/`.
  Copy, never symlink into the loom checkout. Re-check admission on the next tick.
- **Stop condition**: once per root per window. If it recurs on the same root
  after a backfill, that is a real bug — escalate (E4) and file it rather than
  re-copying every tick.

### R3 — Token pool degraded or ranking stale

- **Trigger**: healthy/total below the dispatch cap's needs, exhausted accounts,
  or a `.ranking` older than ~2× the self-refresh cadence.
- **Verification**: `loom-daemon tokens check --ranking` (this both measures and
  refreshes). Distinguish **stale ranking** (bookkeeping — refresh fixes it) from
  **genuinely exhausted accounts** (weekly limits — nothing local fixes it).
- **Action**: refresh the ranking. If every *pinned* account is failing, the
  selector's `--auto-unpin` pre-flight is the intended release valve — prefer
  letting the spawn path handle it over hand-editing `.allowlist`.
- **Stop condition**: one refresh per tick. **All accounts dead persisting past
  one tick is an escalation (E1), not a remediation** — refreshing a ranking of
  exhausted accounts changes nothing, and repeating it hides the outage.

### R4 — Fleet-wide starvation (dispatch idle, backlog non-empty)

- **Trigger**: in-flight sweeps at/near zero for two consecutive ticks **and**
  every root's ready (`loom:issue`) queue empty **and** a non-trivial
  `loom:curated`-but-unpromoted backlog exists.
- **Verification**: the daemon is alive and healthy (R1 clear), the work finder is
  actually enabled (`status` shows it on — a FLAGS-OFF daemon is *supposed* to be
  idle, and "fixing" that by promoting issues is wrong), and the curated issues
  are genuinely unpromoted — **filter out** anything already carrying
  `loom:issue`, `loom:building`, `loom:blocked`, or `loom:operator-only`. That
  queue is mostly already-promoted noise.
- **Action**: promote a **small** batch (≤3 per tick) of clearly-ready curated
  issues to `loom:issue` using Champion's promotion criteria
  (`champion-issue-promo.md`), with a one-line rationale comment on each. Never
  invent labels; never promote something a human parked (`loom:operator-only`,
  or an issue whose last comment defers it).
- **Stop condition**: ≤3 per tick, ≤10 per window. If the backlog is empty too,
  the fleet is *done*, not starving — say so in the summary and stop promoting.

### R5 — Orphaned claims / stranded sweeps

- **Trigger**: `loom:building` issues with no live sweep and no open PR, present
  across two consecutive ticks (the classic aftermath of a plain daemon restart:
  surviving children become orphans invisible to the registry).
- **Verification**: cross-check the claim against `list_sweeps` / the run
  registry, an open PR for that issue, and the worktree's mtime. A minutes-old
  claim with a warm worktree is a *live* builder — leave it alone. `loom:issue`
  plus an open linked PR is a **safe** state, not a defect.
- **Action**: `loom-recover-orphans --recover` (or the equivalent
  `recover-orphaned-shepherds.sh` path) — nothing hand-rolled.
- **Stop condition**: once per window; recurring orphaning is an escalation (E4).

### Never, under any circumstance

- **Never** restart, stop, `bootout`, or `kill` a daemon on the `status` liveness
  line, the watchdog-provisioned line, or any single launchd probe (#4694).
- **Never** run a bare `loom-daemon-start.sh` as a repair (FLAGS-OFF downgrade,
  #3911) — always `--from-config`.
- **Never** `kill -9` a `loom-daemon` or a sweep child to "clean up". Sweeps are
  independent detached processes that are *designed* to survive a daemon
  restart; killing them strands `loom:building` claims.
- **Never** cancel in-flight sweeps to make a metric look better.
- **Never** edit code, merge PRs, or push branches from the watch loop. If the
  fleet needs a code fix, file an issue and let the pipeline build it.
- **Never** create labels, or apply a label outside `.github/labels.yml`.
- **Never** exceed the per-window stop conditions above by "trying once more".

---

## Escalation contract

Two dispositions. Everything is one or the other — there is no silent third
option where the watch notices something and does nothing about it.

### Self-heal and log (no page)

- Any single-tick degradation that clears by the next tick (transient forge 5xx,
  one rate-limited token, a role tick that lost a race).
- Any R1–R5 action that succeeded and stayed fixed.
- Queue empty with an empty backlog (fleet is idle because there is no work).
- A `HEALTH_MODE=fallback` downgrade (note it once in the summary; not a page).

These land in the tick log and the end-of-window summary. They do not wake anyone.

### Page the operator (escalate immediately)

| ID | Condition | Why it pages |
|----|-----------|--------------|
| **E1** | **All tokens dead/exhausted persisting past one tick** | Nothing local fixes a weekly limit. Every subsequent tick is guaranteed-idle; the window is effectively over until a human acts. |
| **E2** | **Repeated daemon death** — a third death in the window, or a death recurring within one interval of a successful restart | Something is killing it (crash loop, supervisor misconfig, another daemon). Restart #3 is not a fix, it is a mask. |
| **E3** | **Red `main` + halted dispatch** — the main-health gate is halting dispatch and `main` is genuinely broken | The pipeline cannot make progress *by design* until `main` is repaired; that repair is (usually) a human decision about revert-vs-forward-fix. |
| **E4** | **A remediation's stop condition was reached and the condition persists** (e.g. R2 recurring after a backfill) | The playbook is out of moves. Continuing would be repetition, not repair. |
| **E5** | **The watch itself cannot continue** — probe unavailable *and* fallback failing, or the loop is out of context/budget | A silently-dead watch is worse than no watch; say so before stopping. |

**How to page**: post the escalation to the operator's channel if one is
configured (`./.loom/scripts/fleet-send.sh`, see
[`.loom/docs/safehouse.md`](../../../.loom/docs/safehouse.md)), **and** print it
prominently in the session, **and** carry it into the end-of-window summary's
Incidents section. If no channel is configured, the session output plus the
summary is the page — never swallow it because the fancy path is unavailable.

**Page once per condition per window**, not once per tick. Re-page only if the
condition's character changes (E1 for 1 dead account → E1 for the whole pool).

**After paging**: keep ticking (an escalated fleet still deserves observation),
but **stop attempting that condition's remediation**. The watch does not
negotiate with a condition it has already escalated.

---

## Loop mechanics

Three execution contexts, three different correct answers. **Identify which one
you are in before the first tick** — getting this wrong is the failure mode this
skill exists to prevent.

### A. Interactive session, no blocking stop hook (preferred)

Use `ScheduleWakeup` between ticks. Arm the next tick, end the turn, wake, tick,
re-arm. Context cost per tick is one probe plus one line.

> **Expected friction — the background-subagent stop guard.**
> `guard-background-subagents.sh` (#4462/#4696) blocks turn-end **once per stop
> sequence** when it sees an armed, unfired `ScheduleWakeup`. For a watch loop
> this is a **false positive by construction** — the armed wakeup *is* the loop.
> The guard blocks at most once, so simply stopping again is correct and safe.
> Do not `TaskStop` the wakeup to satisfy the guard; that cancels the watch.

> **Hazard — an armed wakeup only fires into a live session (#4930).** A
> `ScheduleWakeup` needs the *Claude session*, not just the host, to be running
> when it fires. If the session suspends — laptop lid closed, terminal app
> quiesced, host UI session goes idle — the wakeup does not error and does not
> get dropped; it simply fires late, whenever the session next resumes, with no
> warning anywhere. `check-host-sleep.sh` (tick-0 preflight, above) does **not**
> cover this: the host itself can stay fully awake — running the daemon, merging
> PRs, ticking every other role — while the one session holding the watch's
> armed wakeup is suspended. A tick that was supposed to fire at 00:57 firing at
> 09:29 with a perfectly healthy fleet in between is this failure mode, not a
> host-sleep miss.
>
> **Tick-0 preflight addition.** Before choosing mode A, confirm the session
> will stay live for the *entire* window — an always-on host with a detached/
> backgrounded session that will not suspend. If that is not true (an
> interactive laptop session that may sleep, lock, or be closed), prefer mode C
> instead: a scheduler-driven headless single tick (`cron` →
> `claude -p "/loom:watch --max-ticks 1"`), so each tick is dispatched fresh by
> something that does not depend on a session staying alive. Say which mode was
> chosen, and why, in the tick-0 plan.
>
> **Diagnostic signature.** If a wakeup fires hours late *and* the health
> probe's own UTC `at` timestamp disagrees with the tick boundary you expected,
> that combination is session suspension, not a timezone bug in the probe or a
> drifting interval — do not spend time chasing a TZ conversion; check whether
> the session was actually live across the gap.
>
> **The gap is bounded, not dangerous.** A suspended session is not an outage:
> the daemon and `loom-daemon-watchdog.sh` (#4011) carry the fleet on their own
> independent of this skill — they do not depend on the watch ticking. The
> watch resumes with its tick-0 baseline intact once the session wakes; there is
> no state to reconstruct. The end-of-window summary should still **disclose
> the gap** (how long the session was suspended, and what the fleet did
> unsupervised during it) rather than silently reporting a clean interval.

### B. A `/goal`-style duration stop hook is active (the deadlock)

**This is the incident #4762 was filed for.** A duration/condition-based session
goal installs a Stop hook that blocks every turn-end until its condition holds.
An armed `ScheduleWakeup` can only fire *after* a turn ends. The goal will not
let the turn end; the wakeup cannot fire; the loop cannot advance. Deadlock — and
it is silent, because both halves are individually behaving as designed.

**Resolution, in order of preference:**

1. **Do not stack them.** `/loom:watch --until 07:00` *is* the "keep the fleet
   running until morning" goal. If the operator asks for both, say plainly that
   the goal hook and the wakeup loop are mutually exclusive and offer the watch
   alone. This is the recommended answer.
2. **If a goal hook is already active and cannot be cleared**, bridge in-turn:
   run the loop *inside a single turn* with bounded waits between ticks
   (`sleep`-and-poll in-turn, never an armed end-of-turn timer), and shrink the
   window or lengthen the interval to fit the turn budget. This is what the
   reference night actually did — ~45 in-turn 10-minute waits — and it works, but
   it costs roughly an order of magnitude more context per tick than (A). Prefer
   fewer, longer ticks in this mode, and be explicit with the operator that the
   watch will end when the turn's budget does.
3. **Recorded recommendation for the goal-hook implementation** (not something
   this skill can do on its own): an **armed, self-rescheduling wakeup should
   count as "condition in progress"** and permit turn-end. A goal whose condition
   is "keep doing X until T" is satisfied *by the existence of the armed next
   tick*, not by the turn continuing. Until a goal implementation adopts that
   rule, (1) and (2) are the only correct answers.

### C. Headless `claude -p` (non-interactive)

**Never arm a `ScheduleWakeup` or a `Monitor` here.** Turn end terminates the
process, killing every background child — the timer has no session to wake, the
exit code is 0, the wrapper logs "completed successfully", and the watch silently
never happened (the #4462 signature). In headless mode:

- Run **exactly one tick** and exit (`--max-ticks 1` is the implicit default), or
- Run a bounded in-turn loop as in (B), then exit.

A cron-driven repeated tick belongs to the scheduler, not to an armed timer
inside a one-shot process.

### Interval discipline

Sleep to the *next tick boundary*, not `interval` from when the tick finished —
otherwise remediation time makes the loop drift. If a tick overruns its own
interval, skip to the next boundary and note the skip; never run two ticks
back-to-back to "catch up".

---

## Dry-run mode

`--dry-run` runs **exactly one tick** and prints what it *would* do. It is the
right way to try this skill for the first time, and the right way to check the
playbook's wiring on a fleet you do not want to touch.

**Permitted**: the health probe (or the fallback battery), `loom-daemon status`,
`pgrep`, reading the pid file / `.ranking` / logs, read-only `gh ... list` /
`gh ... view`, and `test -d`.

**Forbidden — no exceptions**: `loom-daemon-start.sh` / `-stop.sh` / `restart`,
`launchctl` / `systemctl` mutations, `tokens check --ranking` (it *writes*
`.ranking` — use a plain `tokens check` or read the file), any `mkdir`/`cp` into
`.loom/runtimes`, any `gh issue edit` / `gh pr` mutation, orphan recovery, and
any escalation page (report the *would-page* in the output instead).

Output shape:

```
DRY RUN — 1 tick, no mutations
Probe:        native (loom-daemon health) — exit 1 (degraded)
Liveness:     alive (pid 13724, pidfile agrees)          [pgrep+pidfile, #4694]
Dispatch:     6 in flight, cap 8 (binds: maxConcurrent)
Tokens:       5/7 healthy, ranking 4m old
Roles:        1 persistent failure (curator @ repo-b), 3 transient (ignored)
Queues:       loom  2 ready / 6 building   ·  repo-b  0 ready / 1 building
Throughput:   4 merges in the last 25m

WOULD DO:     R3 (token ranking refresh) — 2 accounts exhausted, ranking stale
WOULD NOT:    R1 — liveness verified alive; status line alone is never a trigger
WOULD PAGE:   nothing
```

---

## End-of-window summary

Print this when the window closes (`--until` reached, `--max-ticks` exhausted, or
the operator stops the loop). This is the artifact of the whole window — if the
watch produces nothing else, it produces this.

```
Fleet watch — 22:14 → 07:01 (21 ticks, 25m interval)

MERGES BY REPO (42 total)
  loom            28
  anvil            7
  repo             5
  kicad-tools      2

FLEET ISSUE FLOW
  opened          19   (14 by Architect/Hermit, 5 by Auditor)
  closed          31   (24 via merged PR, 7 closed not-planned)
  net backlog     -12  (open loom:issue 9 → 4)

SAVES & INCIDENTS
  02:41  SAVE   #4694 false-dead: status said dead, pgrep+pidfile said alive
                (6 sweeps in flight) — no restart taken
  04:07  SAVE   #4694 false-dead (second occurrence) — no restart taken
  05:12  FIX    R2 #4688 runtimes backfill on 1 root, pre-emptive (0 rejections
                observed after)
  03:55  NOTE   1 transient role-tick failure (curator @ repo-b) self-healed
  —      PAGES  none

TOKEN CONSUMPTION
  pool            7 accounts, 5 healthy at window close (2 exhausted)
  ranking         refreshed 3× (02:41, 04:07, 06:30)
  watch cost      ~21 probes + 3 remediations

STATE AT CLOSE
  daemon          alive, pid 13724, up 8h47m, work finder ON, health gate ON
  in flight       3 sweeps
  queues          loom 4 ready / 3 building · others idle
  verdict         HEALTHY — no operator action required
```

Rules for the summary:

- **Every escalation appears**, even if it later cleared. A paged condition that
  self-resolved is still the most important thing that happened.
- **Saves count.** A tick where the correct action was *not acting* (an averted
  #4694 restart) is a result, not a non-event — it is the single highest-value
  thing this loop does, and it is invisible unless reported.
- **Counts come from the forge**, diffed against the tick-0 baseline — never from
  the watch's own memory of what it saw.
- **Close with a one-word verdict** and an explicit "operator action required:
  yes/no". If yes, say exactly what.

---

## See also (cross-links, not duplicated here)

- [`.loom/docs/troubleshooting.md`](../../../.loom/docs/troubleshooting.md) →
  **Overnight / long-running orchestration** — host sleep (`check-host-sleep.sh`,
  #3350) and keeping installed `.loom/` copies fresh after a pull
  (`resync-installed.sh`, #3770/#3777/#4239). The watch assumes those; it does
  not restate them.
- [`.loom/docs/daemon-reference.md`](../../../.loom/docs/daemon-reference.md) →
  **Autonomous work finder**, **Config surface (`.loom/config.json →
  autonomous`)**, **Safe start / stop**, and **Autonomy-loss watchdog +
  heartbeat (#4011)** — the authoritative description of every knob and process
  this skill observes.
- [`.loom/docs/token-pool.md`](../../../.loom/docs/token-pool.md) — pool provisioning,
  ranking, and what "exhausted" actually means.
- `defaults/docs/guard-hooks.md` → **Background Subagent Stop Guard** — why an
  armed wakeup trips a stop once, and why that is expected here.
- `/loom:loom` — the operator surface for driving the daemon directly
  (dispatch/cancel). The watch observes; `/loom:loom` commands.

## Limitations

- Remediation is deliberately a **closed set** (R1–R5). A condition outside it
  escalates; the watch does not improvise repairs on a sleeping operator's fleet.
- Single-host. It watches the daemon(s) reachable from this checkout; a
  multi-host fleet needs one watch per host (or a dispatcher-level equivalent).
- The end-of-window summary is only as good as the tick-0 baseline — a watch
  started mid-window reports the window it observed, not the night.
