# Merge sequencing (`loom:sequenced`)

`loom:sequenced` is the durable "approved, but not yet" ordering hold (#9378).
While it is on a PR beside `loom:pr`, `merge-pr.sh` refuses the merge, with no
override flag. The condition it waits on is a trusted
`<!-- loom:sequence after=… pred_head=… follower_head=… plan=… -->` marker on
the PR. The daemon's merge-sequencing pass (#9686) writes both the marker and
the label, re-evaluates them every tick, and removes the label when the
recorded predecessor lands, closes, or the ordering no longer applies.
Read-only inspection: `loom-daemon merge-pr sequence-plan`.

## Conflict stacking and the sequencer (#12759)

With `stacking.onConflict.enabled` (#12758), a Builder whose committed work
really conflicts with an open Loom PR P is replayed onto P's branch and opens
its PR with P's branch as base (`loom-daemon stack-parent`, run from
`builder-pr.md` before the pre-PR gate). The test is this pass's own pairwise
`merge_tree_conflicts` (a `CHANGELOG.md`-only conflict is clean), but read the
opposite way: an unknown (`None`) result never stacks, where the sequencer
treats it as a conflict.

- **Choice.** Among eligible conflicting PRs (open, not draft, a
  `feature/issue-N` / `feature/harness-ops-N` branch, no hold label and no
  `loom:changes-requested`, depth within `maxDepth`): the top of one chain if a
  single candidate has all others below it, otherwise the oldest by
  `(created_at, number)`, the sequencer's own tiebreak.
- **Interaction.** A stacked child is a follower of its parent through the
  existing `StackedBase` edge, so once the plan triggers (more than two open
  PRs) the child is held `loom:sequenced` behind P. Conflicts stacking did not
  absorb (siblings, a PR at `maxDepth`) are sequenced as above.
- **Parent merges.** The child retargets to the default branch and the
  post-merge reconcile rebases it. `reconcile-child` defers a `loom:building`
  child only while its issue lease is live (`route_with_lease`); the label alone
  stays on until the PR merges.
- **Parent closes unmerged.** In a merge-mode repo the child is flagged
  (`loom:merge-conflict`) and Doctor recovers it with a replacement PR
  (`orphan-stack-replacement.md`, #12767), never a rewrite or a retarget. Keep
  `stacking.onConflict.enabled` **off fleet-wide** until that path has been
  qualified on a real orphan, or the operator accepts the flag-only behaviour.
- **#11708 / #12344.** Stacking does not depend on their ruling. If either
  deletes or simplifies the sequencer, `merge_tree_conflicts` must stay: the
  stacking verb uses it.

## Tick cadence: "the next tick" is minutes, not seconds (#12595)

The pass runs inside the daemon's claim-reconciliation pass, so a "tick" is
`LOOM_CLAIM_RECONCILE_INTERVAL_SECS`: **600s by default, 1800s when
`safehouse.enabled`** (see `daemon-reference.md`). When a predecessor gains
`loom:blocked`, `loom:operator`, `loom:changes-requested` or `loom:ci-failure`,
its soft followers are released on the first tick that reads the new label, so a
follower can legitimately stay `loom:sequenced` for up to one interval (30 min on
a safehouse fleet) after that. Longer than one interval is a defect: run
`loom-daemon merge-pr sequence-plan`, whose `held #F (after #P): <why>` lines name
the first `with_readiness` condition keeping each hold (human hold, predecessor
outside the listing or moved off the recorded head, stacked base, or
"predecessor is ready"). It writes nothing.

## A `CHANGELOG.md`-only conflict creates no landing order (#12595)

Two PRs that both add an entry at the same spot of `CHANGELOG.md` do conflict
textually, but the Doctor resolves that on the rebase; ordering their merges
saves nothing. The pair check (`git merge-tree --write-tree --name-only`) treats
a conflict whose **only** conflicted path is `CHANGELOG.md` as a clean merge. Any
other conflicted path, or an unavailable path list, keeps the edge (fail closed).
Stacked-base edges are unaffected.

## One landing-order comment per PR (#10634)

The "Landing order recorded" comment is upserted, not appended. It carries a
hidden key, `<!-- loom:landing-order v1 after=N after_head=… follower_head=… -->`:
the PR's order (after #N) pinned at both heads. The component's `plan=` id is
not part of the key, because any push to any PR in a large component changes
it. On each pass the PR's trusted comments are read once and:

- the same key already on the thread, with no sequencing note after it, is
  left alone (no write);
- a changed key edits that comment in place;
- otherwise (no landing comment yet, or a release/replan note or newer marker
  follows it) one new comment is posted;
- an older fleet-authored landing comment whose key a newer one repeats is
  deleted, a few per pass. This is how two hosts that both posted converge.

Comments written before the key existed are recognized by their heading and
`source=pass` marker. Only fleet-authored comments are edited or deleted. A
failed comments read skips the PR for that pass rather than posting.

One host per workspace runs the pass: the role-runner shard
(`autonomous.roleRunner` `shardIndex`/`shardCount` or the roster) that already
decides which host owns a workspace. An unsharded host owns every workspace, so
a single daemon is unaffected, and the duplicate cleanup above covers a fleet
that is not sharded.

## Only a landable predecessor anchors a hold (#12010)

A PR is only ever held behind a predecessor that is ready to land: `loom:pr`,
with no `loom:changes-requested`, `loom:ci-failure`, `loom:blocked` or
`loom:operator` (#10371), not itself sequenced behind such a PR (#10465), and
**landable now**. Each tick the pass reads every such approved PR's check runs
on its current head and its mergeability, both as conditional (ETag'd) REST
reads that cost nothing for an unchanged head. A PR with a completed `failure`
or `timed_out` check, or that GitHub reports as conflicting with its base, is
treated as not ready: no new hold is planned behind it, and an existing soft
hold behind it is released on that tick, with a comment naming the red checks
or the conflict. Pending CI, a `cancelled` run, and unknown mergeability change
nothing, so a freshly re-dated head does not churn holds. Human holds (no
`source=pass`), stacked bases and consolidation reservations are unaffected.
Once the predecessor is green again it is ordered oldest-first as before.

## A pinned head that moves

By default a hold whose follower head moved since the marker was written is
voided and re-planned on the next tick, because the marker no longer describes
the tree it holds.

**A tree-identical re-date is not a moved head (#10398).** A tree-identical
commit (such as the #8508 re-date push, retired by #12063) moves the head
without changing the tree. If the forge proves the new head's tree equals the
pinned head's tree (the same `kind=tree` test that carries a review verdict
across a re-date), the pass keeps the PR's sequencing state as it is:

- a held PR keeps its hold, its plan and its `pred_head`, and gets no replan
  note and no new "Landing order recorded" comment;
- a PR an operator released (see the next section) stays released.

A failed or negative comparison keeps the old behavior (void and re-plan).
`LOOM_VERDICT_TREE_CARVEOUT=0` turns the comparison off for this pass and for
the verdict carve-out together.

## An operator removing `loom:sequenced` is a sticky release

When someone outside the fleet removes `loom:sequenced` from a PR, that is a
deliberate release of that ordering. The pass records it on the PR
(`<!-- loom:sequence operator-released after=N follower_head=… -->`) and does
not re-create the edge to the same predecessor (`after=N`) **until the PR's
tree changes**. A tree-identical re-date does not end the release. A real
content change does, and from then on the order is re-derived as usual.

The release is detected only on positive evidence: the newest
`loom:sequenced` label event on the PR is a removal, by an actor that is not
one of the fleet's identities, made strictly after the comment that wrote the
PR's newest sequence marker, and that marker has no release or replan note
after it. A release made by the pass itself always writes such a note, so it
keeps its existing behavior. An unreadable label history, an event with no
actor, a fleet actor, a removal older than the marker's comment (it released
an earlier hold), or a missing timestamp records no sticky release. Fleet
identity comes from the host's fleet login roster: a daemon acting under a
login the roster does not list can have its own removal read as an
operator's.

The release is scoped to the (PR, predecessor) pair. An edge to a different
predecessor, such as an ADR-0023 consolidation reservation, is unaffected.
Removing the label is the supported way to release an order. `merge-pr.sh`
still has no flag that bypasses a label that is present.
