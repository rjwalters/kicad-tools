# Merge-Queue Authorization Protocol

**Issue #10256** (Phase B1 of #9978). Implementation:
`loom-daemon/src/forge_merge_queue/authz.rs`; fake-forge tests:
`authz_tests.rs`.

**Status: design and state machine only. Queue execution stays dormant**
(`QUEUE_EXECUTION_ENABLED = false`, `champion.mergeMode` defaults to `direct`).
Nothing calls this module yet, and the direct merge path is unchanged.

## Problem

GitHub merges a queued PR on its own schedule, independent of Loom's labels.
Verifying approval once and cleaning up on a later daemon tick leaves PRs
mergeable after approval was revoked. The protocol must be enforced by the
forge, not by Loom's cadence.

## Protocol

1. **Authority is a grant.** A grant binds one PR to one approved head SHA and
   lives in a `GrantStore`. Default state is deny.
2. **The required check is the enforcement point.** Queue mode requires the
   `merge_group` status context `loom/merge-authorization`. Its body
   (`merge_group_check`) succeeds only when a grant exists for the PR's current
   head and the live facts still pass `evaluate`: head unchanged, approval label
   present, verdict current, no reviewing claim, no human hold, no contradicting
   label. Any fact that cannot be determined, an unreachable grant store, and a
   forge error all conclude `Failure`. A required check that fails, errors or
   never reports blocks the merge, so outages block rather than allow merges.
3. **External changes need no Loom cooperation.** A human removing a label,
   adding a hold or force-pushing changes the live facts the check reads.
4. **Loom-owned transitions revoke first.** `revoke_then_dequeue` revokes the
   grant, then dequeues; both are always attempted and both are idempotent.
   `Revocation::safe_to_transition` is true only when the grant is confirmed
   revoked or the PR is confirmed out of the queue; only then may verdict
   invalidation, a reviewing claim or `loom:operator` proceed.
5. **Enqueue re-validates after the write.** `authorize_and_enqueue`: gate,
   evaluate, grant, enqueue pinned to the approved head, evaluate again; a
   revocation that raced the enqueue is rolled back in the same call.

## What is demonstrated (fake forge)

Unauthorized PRs cannot merge in these cases: no grant; revocation before
enqueue; head race (forge rejects the pinned enqueue, grant rolled back); head
moved after enqueue; revocation while checks are pending; failed dequeue after
revoke; grant-store outage; forge-fact outage; each external change (label,
verdict, claim, hold, contradiction) with no daemon involvement; duplicate
revocation events; a stale grant after a head move.

## Known gap (why the mode stays disabled)

The window between the required check passing on the merge-group commit and
GitHub performing the merge cannot be closed by any Loom-side step: GitHub has
no atomic evaluate-then-merge hook. A revocation that completes inside it, with
a failing dequeue, can still merge. The test
`known_gap_revocation_after_check_pass_can_still_merge` pins this, and
`INVARIANT_FULLY_DEMONSTRATED` is `false`. Enabling queue mode requires either
a forge-side mechanism that closes the window or an explicit operator decision
to accept it; neither is in scope here.

## Not in this slice

Wiring into Champion (`champion-pr-merge.md`), queue-drop reconciliation with
verified removal reasons, confirmed-merge gating of issue close and cleanup,
deduplicated telemetry, a durable `GrantStore`, and the `merge_group` check
workflow. Production enablement also needs Phase C qualification
(`merge-queue-ci.md`).
