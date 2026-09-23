# Verification ownership and in-flight visibility (#8268)

Two agents running the same 15-minute test suite against the same tree produce
one answer and consume thirty minutes. This document is the reference for why
that keeps happening, which half of it is a *rule* and which half is a *missing
input*, and the mechanism Loom ships for the second half.

## The measured failure

From a ~10-agent session in `2AMLogic/gf180-parasynth`:

An agent finished its deliverable, started a background verification, **ended
its turn (correct)**, was re-invoked when the job completed (correct), reported,
and **started another verification**. It did this roughly six times across **72
minutes** for work that took perhaps 40. Meanwhile its coordinator was running
the same suite, because the agent had flagged a check for review. A third run
was started after a rebase. **Three copies of one 15-minute suite, for one
answer.**

Every individual decision was locally correct. Verify before reporting is
diligence. Ending the turn rather than polling is what the guidance asks for.
Re-verifying after a rebase is right. **The agent was not misbehaving** — which
is exactly why adding another "be more careful" instruction would not have
helped.

## Two different problems, two different fixes

| Problem | Nature | Fix |
|---|---|---|
| An agent tries to be **the last word** on a merge-bound branch | An ownership question — answerable by a rule | The ownership rule below |
| An agent **cannot see** that its coordinator is already running the same command | A missing input — no amount of discipline supplies it | `loom-daemon inflight` |

Shipping only the rule is the mistake #3707 made about concurrent issue filing,
documented in [`issue-filing-lock.md`](issue-filing-lock.md): a convention that
asks an agent to account for something it cannot observe is not a weak fix, it
is a non-fix, and it fails the first time the unobservable thing happens.

## The ownership rule

**Verification of a merge-bound branch belongs to the downstream gate, not to
the agent that produced the branch.**

Concretely, in Loom's lifecycle:

| Artifact | Who owns verifying it | Who must NOT re-verify it |
|---|---|---|
| A worktree's local build before commit | Builder (its own `buildGate`) | — |
| CI on a pushed PR | **Judge** | Builder, after hand-off |
| A PR after a Doctor fix | **Judge**, on the re-review | Doctor, after pushing |
| A wave's merged result on `main` | The coordinator / `/loom:sweep` | Any wave child |

A producer's obligation ends at: **commit, report the deliverable, stop.** An
agent cannot know what else is running, so it should not try to be the last
word. This is not new policy — `builder.md` already states it as the *correct
default* rather than a fallback, in "CI on a PR you just pushed":

> Once the PR exists with `loom:review-requested`, verifying CI is **Judge's**
> gate, not yours. Push, create the PR, state in your final message that CI was
> still running at hand-off, and move to the next issue.

What #8268 adds is the reason it is load-bearing even when you have spare time
to wait: the wait is not free, and the cost is not tokens. The author measured
an idle wake at only **~500–2,000 tokens** — prompt caching works — so an
optimisation aimed at token spend would have been aimed at the wrong thing. The
cost is **wall-clock and coordination**: a busy CPU, a slot another agent
needed, and a notification stream that makes a 40-minute job look like a
72-minute one.

## The mechanism: `loom-daemon inflight`

A machine-wide registry of long-running commands, keyed by a fingerprint of
**(command, tree, branch)**. It answers the question the rule cannot: *is this
already running?*

No daemon process is required — state is an on-disk directory
(`~/.loom/locks/inflight` by default, `LOOM_INFLIGHT_DIR` to relocate). The
sessions this coordinates routinely run on hosts with no daemon at all.

### Before launching a long verification

```bash
# Exit 0 => you own it, run the command. Exit 10 => already in flight.
FP=$(loom-daemon inflight claim \
      --command "pnpm check:ci" \
      --tree "$PWD" \
      --branch "$(git branch --show-current)" \
      --agent "builder/issue-8268")
case $? in
  0)  pnpm check:ci; rc=$?
      loom-daemon inflight release "$FP"
      exit "$rc" ;;
  10) echo "Already running — reporting the in-flight run instead of duplicating it." ;;
esac
```

`claim` prints the fingerprint on stdout and the holder's identity on stderr, so
the "already running" branch has something concrete to report: which command,
which tree, which branch, which agent, and how long ago it started.

### To report, not to decide

```bash
loom-daemon inflight list          # everything in flight on this host
loom-daemon inflight check --command "pnpm check:ci" --branch main
```

**`check` is advisory and `claim` is not.** This distinction is the whole of
#8268's race question, so it is worth stating plainly rather than burying in a
"known limitation" note:

- A bare read has an unavoidable check-then-launch window. Two agents ask at the
  same moment, both see clear, both launch — reproducing the exact duplication
  the registry exists to prevent.
- `claim` has no such window. The entry is a directory created with `mkdir`
  (POSIX-atomic across processes and languages, the same primitive
  [`build-slot`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/build_slot.rs)
  and the per-issue claim lock use). Exactly one of N simultaneous claimants
  wins; the losers are handed the winner's record.

So: **use `claim` when you intend to launch. Use `check`/`list` when you are
reporting fleet state.** A coordinator surfacing "3 suites in flight" wants
`list`; an agent about to start a suite wants `claim`.

### What it deliberately does not do

- **It does not normalize semantics.** `cargo test --all` and `cargo test
  --workspace` are different entries even when they run identically; only
  whitespace is folded. A normalizer clever enough to merge them would
  eventually suppress a verification someone needed — strictly worse than the
  duplicate run it saved.
- **Different trees are different entries**, always. Two builders running the
  same command in `.loom/worktrees/issue-A` and `issue-B` are doing different
  work and must not deduplicate. The registry is machine-wide precisely so the
  *same* tree is shared across checkouts (a coordinator in the primary clone
  and a subagent in a worktree see each other), not so different trees collide.
- **Different branches are different entries.** Re-verifying after a rebase is
  correct; the fingerprint changes with the branch, so it is allowed.
- **It never blocks.** An unusable store degrades open and the command runs
  unserialized. Duplicated work is the failure this reduces; *skipped*
  verification would be a worse one it must never cause.

### Staleness

An entry is reaped when either leg fires: its owner PID is dead (the immediate,
common case — an agent killed mid-suite that never ran `release`), or it ages
past `LOOM_INFLIGHT_STALE_SECS` (default 4h). The age threshold is long on
purpose: entries describe whole test suites, and a short threshold would let a
peer declare a healthy 20-minute run "stale" and duplicate it.

### Relationship to `build-slot`

They compose and do not overlap. [`build-slot`](build-gate.md) bounds *how many*
heavy commands run at once; a slot is anonymous, so two agents running the
identical suite against the identical tree take two slots and produce one
answer. `inflight` bounds *how many copies of the same answer* are being
computed. Neither requires the other.

### Second consumer: a claim also fences your worktree (#8413)

A registration is not only "this command is already running" — it is **evidence
that a live agent is working inside `--tree`**, and since #8413 the daemon's
destructive worktree passes read it as exactly that:

| Pass | What a live claim on the tree does |
|---|---|
| Worktree reaper (`#4876`) | Downgrades `Remove` / `RemoveWithQuarantine` to "skip: in use" |
| Mid-build watchdog (`#4449`) | Resolves to `InUse` — no `git reset --hard`, and the single recovery retry is *not* consumed |

This is the answer to the gap the 2026-09-20 incident exposed: an **in-session
builder** (a Task-tool/operator session, not a daemon sweep) has no claim-lock,
plants no `.loom-in-use` marker, and issues each shell command as a one-shot
subshell, so between commands there is no process for the daemon to see. A
multi-minute `cargo build` writes only into `target/`, which the registry-visible
signals do not watch — so the worktree reads as idle and clean, and a reaper pass
hard-reset one mid-compile, destroying an uncommitted layer.

So if you are an in-session builder about to start a long compile in a worktree
the daemon has no sweep record for, claim it — `--tree` is what fences it:

```bash
FP=$(loom-daemon inflight claim \
      --command "cargo test --workspace" \
      --tree "$WORKTREE_ABS" \
      --branch "$(git -C "$WORKTREE_ABS" branch --show-current)" \
      --agent "in-session builder/issue-<N>")
cargo test --workspace; rc=$?
loom-daemon inflight release "$FP"
```

Containment is component-wise, so a claim on a path *inside* the worktree fences
the whole worktree, while `.loom/worktrees/issue-84` never matches a claim on
`.loom/worktrees/issue-8413`. Releasing (or dying — see "Staleness") un-fences
it, so a forgotten claim cannot wedge the reaper permanently.

**You are not required to claim.** The reaper's removal passes also honour plain
filesystem activity (`LOOM_WORKTREE_ACTIVITY_WINDOW_MINUTES`, default 30m,
including a depth-1 `target/` scan — #8116), which costs an agent nothing and
covers the builder that never registered anything. The claim is the *explicit*
path: it is the only one that fences a worktree the daemon believes is clean and
finished, and the only one the mid-build watchdog's reset honours.

## Reconciling the two background-work rules already in force

A reader meeting `builder.md` and `sweep-execution-model.md` on the same day can
reasonably think they disagree. They do not — they govern different actors — but
the distinction was never written down, so here it is.

| Rule | Who it binds | What it forbids |
|---|---|---|
| `builder.md` / `doctor.md` → "Never End Your Turn on a Background Build or CI Monitor" | A **worker** waiting on *its own* build or CI | Arming a watcher and ending the turn so a future turn reads the result |
| `sweep-execution-model.md` → "Subagent dispatch is async-only" | An **orchestrator** awaiting a *dispatched subagent* | Trusting a sync flag; in headless `-p`, ending the turn at all while a child is unresolved |

The one line that looks like a contradiction is the interactive branch of the
second rule — *"background agents keep running across turns … just end the turn
and let the completion notification arrive on a later turn."* That is advice to
an **orchestrator awaiting a child**, where the notification is the cheapest
correct await. Applied by a **worker to its own build**, it produces exactly the
#8268 loop: end turn, wake, report, launch another check.

**And neither rule addresses the actual #8268 failure at all.** Both are about
*one* agent's relationship to *one* background job. Neither prevents two
independent agents — or an agent and its coordinator — from each running the
same check concurrently, because neither has any way to know the other exists.
That gap is what `inflight` fills, and why the fix could not have been another
sentence in either prompt.

## Where this is referenced

- `defaults/.claude/commands/loom/sweep-run-hygiene.md` → Coexistence, for the
  sweep-start case.
- `defaults/.claude/commands/loom/builder.md` and `doctor.md` → the same-turn
  resolution rule points here for the ownership question behind it.
- `loom-daemon/src/inflight.rs` → the implementation and its unit tests.
