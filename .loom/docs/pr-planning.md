# Human-directed PR preference

A human-directed PR gets the next available opportunity to move toward landing.
An agent used in an interactive session counts: the human drove the work.
Approving a backlog issue for independent fleet execution is autonomous work.

Three concepts remain separate:

| Concept | Meaning |
| --- | --- |
| Origin | Historical fact: `interactive`, `autonomous`, or `unknown` |
| Planning preference | Prefer interactive PRs through review, repair, re-review and merge |
| Priority level | `loom:very-important`, then `loom:important` (#11103), stronger than human preference; drains highest level first (`priorityLevel` on each row: 2, 1, 0; `priorityReason` `priority`). The PR carries the closing issue's level label, copied at creation (#10518). The retired operator-star labels are not read (#11510) |

Set the origin explicitly at session start with `LOOM_WORK_ORIGIN=interactive`,
or at creation:

```bash
LOOM_WORK_ORIGIN=interactive ./.loom/scripts/create-pr.sh \
  --title 'fix: example' --body-file /tmp/pr-body.md --label loom:review-requested
```

`loom-daemon provenance pr-marker --origin interactive` also renders the record
for a manual PR body. The existing v1 provenance marker carries `origin=`.
Daemon sweep/role launches pin `autonomous`; provenance collection treats
`GITHUB_ACTIONS=true` as autonomous ahead of inherited interactive context. Direct unattended callers must explicitly set
`LOOM_WORK_ORIGIN=autonomous`. Unrecorded/legacy work is `unknown`; usernames,
bot flags, coauthor trailers and branch names cannot imply an origin.

Creation preserves an existing provenance record. Adoption returns the existing
PR without rewriting its body. Doctor and subsequent Judges keep that original
record even when their own processes are autonomous. Origin describes the PR's
creation context, not the latest person or agent to touch it.

Only a single well-formed record on a PR authored by a trusted repository
identity earns preference (the existing [comment trust policy](comment-trust.md)).
Missing, malformed, duplicate, conflicting or untrusted records are unknown.
A trusted agent can declare interactive origin; a human author without an
explicit declaration remains unknown.

## Policy and configuration

`planning.preferHumanPrs` defaults to `true` through the normal effective
configuration layers (shared/private fleet defaults and repository overrides):

```json
{
  "planning": {
    "preferHumanPrs": false
  }
}
```

Setting it to `false` restores ordinary PR ordering and fallback admission.
Origin remains recorded and priority labels keep their ordering.
`loom-daemon pr-queue --role judge|doctor|champion` supplies one shared ordered
JSON queue with `origin`, `priorityReason` and `mode` explanations. Role prompts
refresh the queue after each completed/skipped PR and take the next unvisited row.
Keep a per-pass visited set so a waiting preferred PR cannot loop or idle others.
This lets a human PR arriving during a fleet batch take the next free turn. They
use that order, preserving their normal tie-breaks: Judge listing order,
Doctor approved conflicts before review feedback, Champion oldest first.
Priority PRs lead (`loom:very-important`, then `loom:important`); with preference
enabled interactive PRs follow, then ordinary work.
Existing emergency recovery of verified-red main remains a separate higher
priority workflow; this policy changes neither issue/build planning nor recovery.

Preference is non-preemptive. Finish work already running, use normal concurrency,
and never interrupt agents, cancel CI, pause the fleet or take an
extra slot just because a PR is interactive. Every role continues past candidates
waiting for checks, dependencies, a live claim or a required human response.
No quota or aging mechanism is added.

## Discovery is separate from permission

With preference enabled, eligible interactive unlabeled PRs join the Judge's
queue even when fleet review work is waiting, and can open its role-runner gate.
The existing fallback guard still applies bot exclusion, lifetime cap and head
SHA dedup. Its fleet App exception already permits a trusted interactive agent.
Fallback review is comment-only: it grants no workflow enrollment, approval,
branch mutation or automated landing. Existing labeled PRs retain the normal
review, ownership, hold and merge requirements.

An unlabeled, non-draft PR whose head branch is `feature/issue-<N>` in the base
repository also joins the Judge's queue and opens its gate, whatever the queue
depth or `planning.preferHumanPrs` (#9389): a Builder PR that lost its
`loom:review-requested` label is found, in the same comment-only mode, behind
every labeled PR. Same-repository is the forge's head/base repository identity,
never a branch name. Fork PRs and other branch shapes are a deliberate
exclusion: unless interactive, they are still admitted only when no open PR
carries `loom:review-requested`, and nothing here adds a label.

Drafts and holds never gain authority from origin, and every queue drops every
park, `loom:operator` included (#11861). The one exception is Champion's: it
keeps a `loom:pr` + `loom:blocked` PR, its own merge-risk or critical-file hold,
which it re-evaluates; and Doctor's and Judge's keep `loom:changes-requested`
and `loom:review-requested` beside `loom:blocked` when a trusted Champion hold
marker stands (`fetch_queue` reads the comments), so a held PR Champion routes
for repair keeps its hold through the whole round trip. Pending/failing checks can
block a merge while still leaving actionable repair work. The queue is a plan;
fresh claim, CI, dependency and merge guards remain mandatory before acting.

**Judge is not served while CI is pending at the head (#12003).** Judge's own
pre-check releases a PR whose checks are still running, so serving one buys only
a claim, a spawn and a release. `pr-queue --role judge` and the Judge spawn gate
(the role runner's demand probe calls it) therefore skip a PR whose head has
pending CI, and serve it once CI concludes. Both go through one admission helper,
so the queue and the gate agree row for row. "Pending" is the no-verdict
backstop's rule: a check run or legacy commit status not yet concluded that
started under 6 h ago. A run stuck longer, or one with no start stamp, does not
hold the PR, so a head with no runner is still reviewed; an unreadable CI read
counts as not pending, so the skip can only save a spawn, never starve a PR. The
read happens only for a row already admitted, the queue screens only its first
three CI-clear rows (Judge's own pre-check still catches any further down), the
read is conditional (ETag), and the daemon's per-tick gate caches each
(repo, head) answer for 60 s. A push is a new head and so a fresh read. See
[`lease-record.md`](lease-record.md) for how a Judge pass that stood down for
pending CI is released.

**Where the gate lives under step dispatch (#12042).** Since the step-dispatch
cutover (#11909) Judge is drawn as a step, not by the role runner, so the gate
runs in the step tick's gather (`step_dispatch::gather::hold_ci_pending`): a
Judge step whose PR head has pending CI, by the same rule and the same per-head
60 s cache, is a `ci_pending` pass (weight 0, `pick.decision` skip reason
`backoff`) and is drawn once CI concludes. At most four uncached heads are read
per repo per tick; a head past that budget is drawn unread. If a Judge step
still stands down for pending CI, its `noop`/`ci_pending` outcome cools the
`(PR, judge)` pair for the 10 min refusal window, not the 1 h no-op window, so
the next review follows CI by minutes.
