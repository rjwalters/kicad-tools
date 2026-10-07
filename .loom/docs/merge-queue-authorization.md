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

## Phase B2 (#10256): lifecycle, delivered dormant

`forge_merge_queue::lifecycle` adds, behind fake-forge tests: a guarded
`handoff` (all gates then enqueue; refused in direct mode), `reconcile_pr`
(confirms GitHub-reported merge, routes drops by verified reason, reports an
unknown reason as unknown), revocation-then-dequeue before Loom-owned
stale-verdict transitions (`forge disable-auto-merge`, claim-reconciliation
disarm), a daemon-tick sweep, a comment-backed `GrantStore`, a refusal of the
direct re-date remedy in queue mode, and deduplicated enqueue/removed/merged
telemetry. Direct mode makes no forge call from any of these. Issue closure
stays GitHub's `Closes #N` on the confirmed merge; worktree cleanup stays the
reaper's merged-PR pass.

## Champion wiring (#10256, B3)

`champion-pr-merge.md` Step 3 calls `loom-daemon forge merge-queue step <PR>
--approved-sha <head>` immediately before `merge-pr.sh`. `step` is `reconcile`
then, only on `LOOM-MERGE-QUEUE-CONTINUE`, `handoff`. The Champion falls
through to the unchanged direct `merge-pr.sh` call **only** when the first line
is `LOOM-MERGE-QUEUE-DIRECT` on stdout with exit 0. A daemon that predates the
`step` verb can already honor `champion.mergeMode=queue`, so an unrecognized
subcommand alone is NOT permission to merge directly. #10628 allows the
pre-#10256 direct merge, logged as `LOOM-MERGE-QUEUE-COMPAT`, only when direct
mode is *proven*: the same binary's `forge merge-queue mode` prints
`mode=direct`, or the binary has no `merge-queue` verb at all and so predates
merge modes. Every other result (queued, dropped, merged, undetermined, invalid
mode, unprovable mode, empty output) sets `MERGE_RC=7`: nothing merged, the PR
stays approved, no direct merge. The non-queue 7s are surfaced as
`CHAMPION-MERGE-QUEUE-STALL` plus one head-keyed PR notice. See
`merge-pr-exit-code-exceptions.md` → "Exit 7". Because the Champion never calls `merge-pr.sh` in
queue mode, the direct re-date remedy is never run there. Failure mode guarded:
a queue-mode PR merged directly, bypassing the authorization protocol.

## Still not done

The grant store is comment-backed, not durable; no `merge_group` check
workflow; `judge.md` is not wired (review claims and revocations reach the
queue through `forge disable-auto-merge`/claim-reconciliation revoke and the
live `loom/merge-authorization` check); the post-check-pass revocation window
above is still open, so `INVARIANT_FULLY_DEMONSTRATED` and
`QUEUE_EXECUTION_ENABLED` stay `false`. Production enablement also needs Phase C
qualification (`merge-queue-ci.md`).
