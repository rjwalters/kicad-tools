# Human-directed PR preference

A human-directed PR gets the next available opportunity to move toward landing.
An agent used in an interactive session counts: the human drove the work.
Approving a backlog issue for independent fleet execution is autonomous work.

Three concepts remain separate:

| Concept | Meaning |
| --- | --- |
| Origin | Historical fact: `interactive`, `autonomous`, or `unknown` |
| Planning preference | Prefer interactive PRs through review, repair, re-review and merge |
| Operator star | Explicit `loom:operator-priority` override, stronger than human preference; higher levels (`loom:operator-high-priority`, `loom:high-priority-inherited`, #10307) drain first, highest level first (`operatorPriorityLevel` on each row) |

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
Origin remains recorded and operator stars retain their existing behavior.
`loom-daemon pr-queue --role judge|doctor|champion` supplies one shared ordered
JSON queue with `origin`, `priorityReason` and `mode` explanations. Role prompts
refresh the queue after each completed/skipped PR and take the next unvisited row.
Keep a per-pass visited set so a waiting preferred PR cannot loop or idle others.
This lets a human PR arriving during a fleet batch take the next free turn. They
use that order, preserving their normal tie-breaks: Judge listing order,
Doctor approved conflicts before review feedback, Champion oldest first.
Stars lead; with preference enabled interactive PRs follow, then ordinary work.
Existing emergency recovery of verified-red main remains a separate higher
priority workflow; this policy changes neither issue/build planning nor recovery.

Preference is non-preemptive. Finish work already running, use normal concurrency,
and never interrupt agents, cancel CI, pause the fleet or borrow the star's
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

Drafts and hard holds never gain authority from origin. Doctor's existing
`loom:operator` plus `loom:changes-requested` stale-diff repair route remains
available; the operator merge hold stays in place. Pending/failing checks can
block a merge while still leaving actionable repair work. The queue is a plan;
fresh claim, CI, dependency and merge guards remain mandatory before acting.
