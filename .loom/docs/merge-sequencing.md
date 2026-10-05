# Merge sequencing (`loom:sequenced`)

`loom:sequenced` is the durable "approved, but not yet" ordering hold (#9378).
While it is on a PR beside `loom:pr`, `merge-pr.sh` refuses the merge, with no
override flag. The condition it waits on is a trusted
`<!-- loom:sequence after=… pred_head=… follower_head=… plan=… -->` marker on
the PR. The daemon's merge-sequencing pass (#9686) writes both the marker and
the label, re-evaluates them every tick, and removes the label when the
recorded predecessor lands, closes, or the ordering no longer applies.
Read-only inspection: `loom-daemon merge-pr sequence-plan`.

## A pinned head that moves

By default a hold whose follower head moved since the marker was written is
voided and re-planned on the next tick, because the marker no longer describes
the tree it holds.

**A tree-identical re-date is not a moved head (#10398).** The documented
remedy for a stale chain head is to re-date it with a tree-identical commit
and re-run CI (#8248/#8919). If the forge proves the new head's tree equals the
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
