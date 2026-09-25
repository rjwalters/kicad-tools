# `merge-pr.sh` exit codes 3 and 4 — the two "not a failure" outcomes

`merge-pr.sh` reserves two exit codes for outcomes that look like failures to a
naive `|| handle_failure` caller but are not: the merge did not happen, nothing
is wrong, and the correct response is to re-queue the PR for a later pass.

Champion's operative handling lives in
`.claude/commands/loom/champion-pr-merge.md` →
"Exception: exit codes 3 and 4". This file holds the rationale, the design
decisions behind it, and the forensics notes — the parts a Champion session
does not need loaded to act correctly.

| exit | cause | who moved the head |
|---|---|---|
| `3` | The PR's head branch changed between the fresh head-SHA read taken immediately before merging and the merge call itself (#5579). | someone else |
| `4` | The #8248 required-check freshness guard blocked the merge and `--redate-stale-checks` is re-running the stale checks in place (#8914) or re-dated them with a no-op push (#8508). | nobody (in place) / this run (push) |
| `1` | Everything else, including a #8248 block with no remedy left. | — |

## Exit 3 — a foreign push raced the merge (#5579)

`merge-pr.sh` passes the head SHA it read to the forge's merge API as an
optimistic-concurrency precondition. When the branch has moved on, the forge
refuses and the script exits 3 rather than 1: the PR is still Judge-approved,
its diff just moved out from under the attempt. Most commonly a session pushed
new commits to an open, `loom:pr`-labeled branch while Champion was running.

**Diagnostic output.** The script logs to stderr both the stale SHA (the one it
gated the merge on) and the current head SHA, so it is easy to see which
commits raced in. These appear in the `merge-pr.sh` output and the caller's run
log. They are deliberately **not** posted as a PR comment: an exit-3 re-queue is
an ordinary operational event, and commenting on every occurrence would be
noise on a race condition that resolves itself.

## Exit 4 — this run re-ran or re-dated the stale required checks (#8914, #8508)

### What #8248 leaves open

The #8248 required-check freshness guard refuses a merge whose green required
checks started before the base branch's current tip: that result is evidence
about a tree that no longer exists. The guard is correct — it exists because a
ratchet baseline tightened under an in-flight PR and red-lined `main` on
2026-09-18 — and this remedy does not weaken it in any path.

What it left open is that **nothing in the fleet produces the fresh evidence it
waits for.** On 2026-09-21, PR #8493 failed three consecutive Champion merge
ticks with the identical refusal:

- its branch had no new commits, so no CI run ever re-dated its checks;
- `main` kept advancing, so each retry compared against an even later tip;
- `merge-pr.sh`'s internal re-run and a direct `gh run rerun` both failed with
  `Resource not accessible by integration` — the merge token has no
  `actions:write`.

The only recorded escapes were a human merging with an elevated token or a
human pushing a no-op commit. Neither happens automatically, so a
Judge-approved, safety-criteria-clean PR could sit blocked indefinitely — and
invisibly, because Champion's rejection-comment idempotency guard suppresses
the repeated identical failures, leaving no durable record on the PR at all.

### First choice: re-run in place (#8914)

`loom-daemon merge-pr redate-checks` first tries to produce the fresh evidence
**without a commit**: it re-runs, in place, every GitHub Actions workflow run
that holds a stale required check (`POST /repos/{o}/{r}/actions/runs/{id}/rerun`,
the same call `gh run rerun <id>` makes). The head SHA does not move, so the
stale-verdict guard (#5686) has nothing to react to and `loom:pr` survives —
unlike the push below, whose head move costs a full Judge re-review for a
byte-identical tree (PR #8909, 2026-09-25).

- **It re-runs the whole workflow run, not the stale jobs.** GitHub allows one
  re-run per workflow run at a time: after one `POST /actions/jobs/{id}/rerun`
  the run is `in_progress`, every further job re-run in it answers
  `403 The workflow run containing this job is already running`, and jobs not
  re-run are carried into the new attempt with their **original** `started_at`
  (verified 2026-09-25, runs 36145858487 and 36152790007). All required
  contexts here live in the single `ci.yml` run, so per-job re-runs cannot
  refresh them. The whole-run re-run runs every job in parallel: the fast
  required checks come back fresh in about a minute, at the cost of re-running
  the slow non-required suites too (a smaller required-checks workflow would
  make that cheap — #8919).
- **"Already running" is a wait, never a missing permission.** It is a 403
  too, but a push there would throw the verdict away for nothing.
- **It waits, bounded, then merges at once.** It polls until every required
  check is fresh (`--rerun-wait-secs`, env `LOOM_REDATE_RERUN_WAIT_SECS`,
  default 300). Fresh answers exit **5** to a caller that sets
  `LOOM_REDATE_ALLOW_PROCEED=1` — `merge-pr.sh` does, and then merges in the
  same run, which is what gives it a chance against a busy `main` (the
  base-move race is otherwise tracked in #8919). Out of budget answers exit 0
  (`LOOM-RERUN-PENDING`): `merge-pr.sh` exits 4, head and `loom:pr` intact,
  and the next pass continues. A required check that comes back **red** is
  real evidence, not a stale timestamp: exit 1, the refusal stands.
- **It needs Actions: write** on the merge identity. Without it GitHub answers
  `403 Resource not accessible by integration`, and the subcommand falls back
  to the push below — exactly the pre-#8914 behaviour. The same fallback
  applies when a stale required check comes from an app other than GitHub
  Actions (there is no workflow run to re-run). A transient failure (5xx,
  network) is **not** a fallback reason: exit 1, retry next pass.

**Recommended:** grant the fleet merge identity (the `loom-fleet-dispatch`
GitHub App, or a PAT) **Actions: Read and write** — see
`github-authentication.md` → "Required Token Permissions".

### Fallback: the tree-identical push (#8508)

`--redate-stale-checks` makes `merge-pr.sh` perform the remedy the guard's own
refusal text names ("re-run the job, or push any no-op commit to re-date every
check"). `loom-daemon merge-pr redate-checks` creates a commit pointing at the
**same tree** as the current head with the current head as its only parent, and
fast-forwards the branch ref onto it through the Git Data API (`git/commits` +
`git/refs/heads/<branch>`) — no local clone, matching `merge-pr.sh`'s
worktree-safe, API-only discipline. Pushing to the head branch re-triggers
every `pull_request` workflow, which is the fresh evidence #8248 asks for.

It needs no new token grant: the same `contents: write` that `merge-pr.sh`
already uses to sync a base branch into a head branch covers it. That is why it
remains the fallback when the in-place re-run above is refused.

Properties worth stating explicitly, because they are what make this a remedy
rather than a bypass:

- **The diff does not change.** The new commit reuses the current tree
  byte-for-byte, so nothing about what Judge reviewed is altered.
- **No evidence is fabricated.** Nothing asserts a check passed; CI runs for
  real against the new head, and the next merge attempt still has to satisfy
  #8248 on its own terms.
- **It is opt-in.** Without the flag, `merge-pr.sh` behaves exactly as before
  — a human merging by hand never has a commit pushed onto a branch by
  surprise. Champion passes it; nothing else does by default.
- **It never runs on an unknown.** The guard's fail-closed exit (freshness
  undeterminable) does not trigger the remedy — only a positively STALE
  verdict does. `--dry-run` never writes.
- **The head move is honest.** It invalidates the standing Judge approval
  (#5686), exactly as any other push does, so the PR cycles back through
  `loom:review-requested` before merging. That re-review is the correct cost,
  not a regression.

### The bound, and why there is one

If CI on the re-dated head takes longer than the interval between merges on
`main`, the guard is stale again the moment it finishes. An unbounded remedy
would push a fresh no-op commit every tick forever, burning a full CI run and a
Judge re-review each time while never out-racing the base branch.

So the remedy is bounded to **one push per head**. Each push records

```
<!-- loom:stale-check-redate to=<new-sha> -->
```

on the PR. Finding that marker for the *current* head means the full re-date →
CI → block cycle already completed with no forward progress, which is a
strictly stronger signal than a tick counter (and needs no process to own the
count — it is durable forge state).

### Escalation when the bound is reached

The PR is escalated the same way `champion-pr-merge.md`'s merge-risk hold
escalates:

- one idempotent notice keyed on the blocked head
  (`<!-- loom:stale-check-hold head=<sha> -->`), so a later push re-opens the
  question with a fresh notice rather than being silenced by the old episode;
- the `loom:operator` label — the first-class "engine will not act further, a
  human is the only transition out" state (#5502).

`merge-pr.sh` then returns the ordinary exit **1** with the original #8248
refusal: the merge is still refused, and the PR is now simply a held PR that
every `loom:operator` consumer already handles.

**Release** is a human act, by design, and there are two:

1. merge it directly with a token that can re-run the stale check
   (`actions:write`) or that carries elevated merge permission; or
2. push any commit to the branch — that re-dates every required check and
   returns the PR to the normal Judge → Champion path.

Remove `loom:operator` once you have acted. Nothing removes it automatically:
the label is what makes the stuck PR visible and keeps the engine from
re-litigating a state it has already proven it cannot resolve.

## Squash-merge detection trap (applies to both)

If you need to verify by hand whether a re-queued PR's commits actually landed
or were silently stranded, `git merge-base --is-ancestor <commit> origin/main`
is **not reliable evidence either way**: a squash merge produces a brand-new
commit SHA on `main` that is not a git-ancestry descendant of any commit on the
original PR branch, regardless of whether that commit's content made it into
the squash. There is no cheap ancestry check for "squashed-and-landed" vs.
"stranded" — verification requires diffing the actual file content on `main`
against the branch or commit in question.
