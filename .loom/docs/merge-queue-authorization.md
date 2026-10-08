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
to accept it; neither is in scope here. Phase B4 (below) narrows the window
but does not close it.

## Phase B4 (#10256): whole groups, conclude last, re-fail on revocation

`forge_merge_queue::group_authz` (fake-GitHub tests: `group_authz_tests.rs`)
fixes three ways the single-PR check above leaves a revoked PR mergeable:

1. **Grouped merges.** The merge group for the entry at position *k* carries
   every entry ahead of it, and GitHub can merge them all when that group
   passes. `merge_group_check` evaluates only the group's own PR, so a revoked
   PR ahead of it could merge inside a later group
   (`revoked_pr_cannot_ride_a_later_group` shows the old check passing).
   `group_check` evaluates **every member**; any unauthorized member fails
   the group.
2. **Early pass.** `group_check` reports `pending`, which blocks, until every
   other required check on the group commit has succeeded, and only then
   reads the live facts. Until then the live facts are not read, so a
   facts-API or grant-store outage while CI runs also leaves it `pending`;
   the only early `failure` is a definite grant denial (no grant, or a grant
   for another head), which needs no live read. An unknown at the final
   evaluation is a `failure`, never a pass. A hold added while CI runs is
   seen at the final evaluation with no daemon involvement. If nothing
   re-runs the check (daemon or runner outage), it stays pending and GitHub
   times the entry out rather than merging it.
3. **Green but not yet merged** (GitHub's minimum-group-size wait).
   `revoke_refail_dequeue` revokes the grant, posts a `failure` commit status
   for `loom/merge-authorization` on every live group commit that contains
   the PR (statuses are latest-wins per context), then dequeues. Each step is
   attempted regardless of the others, and each is idempotent. The re-fail
   only withdraws authority, so a dormant build does it too.

`GroupRevocation::passed_checks_withdrawn()` reports whether every such
group was re-failed. When it is `false`, a green group may still merge.

**What remains open (pinned as `known_gap_*`):** (a) a revocation after the
final pass while the status API is unreachable and the dequeue fails; (b)
GitHub committing a fully green group before the re-fail arrives. In (b) the
revocation reports `already_merged`, never success. (b) is the same
decide-then-merge interval the direct path has: a label read followed by a
merge call is not atomic either. The fake models GitHub's group semantics as
an assumption, which the Phase C live pilot must confirm. A group-discovery
read that wrongly returns no groups counts as "withdrawn", so discovery must
fail closed (an `Err`), never return an empty list on error.

**Not wired yet:** the GitHub adapters (group discovery from
`gh-readonly-queue/<base>/pr-<N>-<sha>` refs plus queue order, and the
commit-status write), the `merge_group` workflow or daemon pass that runs
`group_check` and posts its status, and switching `revoke_for_transition` and
`reconcile_pr` from `revoke_then_dequeue` to `revoke_refail_dequeue`.

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

The grant store is PR comments. That survives a daemon restart, but it is
not tamper-proof: a trusted author who deletes or edits a revoke comment
brings the grant back. There is no `merge_group` check workflow, and the B4
group protocol is not wired (see above). `judge.md` is not wired either:
review claims and revocations reach the queue through `forge
disable-auto-merge`, the claim-reconciliation revoke, and the live
`loom/merge-authorization` check. The post-check-pass window above is
narrowed by B4 but still open, so `INVARIANT_FULLY_DEMONSTRATED` and
`QUEUE_EXECUTION_ENABLED` stay `false`. Production enablement also needs
Phase C qualification (`merge-queue-ci.md`).

## Group-aware revocation wiring (Phase B5)

`group_github` adds the GitHub adapters (queue entries via GraphQL `mergeQueue.entries`, group commit = entry `headCommit`, members = entries `1..=k`; commit-status write for `loom/merge-authorization`). `revoke_for_root` and `forge merge-queue revoke` now call `revoke_for_transition_groups`, which revokes, re-fails every live group containing the PR, then dequeues, and reports an unconfirmed re-fail as the residual window in the transition comment. Still not wired: the `merge_group` workflow running `group_check`, a durable GrantStore, `judge.md`. `QUEUE_EXECUTION_ENABLED` and `INVARIANT_FULLY_DEMONSTRATED` stay false.

## Check runner (Phase B6)

`forge merge-queue group-check --commit <merge-group-sha>` (module
`group_run`) is the runner that turns `group_check` into the posted
`loom/merge-authorization` commit status: it finds the live group for the
commit, reads the ruleset's other required contexts (newest run per name,
`success`/`neutral`/`skipped` only, truncated listings are errors), evaluates
every member, and posts `success`/`pending`/`failure`. Exit 0 = success posted,
1 = failure, 6 = pending, 3 = nothing confirmed posted; every non-zero exit
leaves the context unsatisfied, so the merge stays blocked. A commit that is
not a live group posts `failure`; a group-discovery outage posts nothing. The
daemon tick also evaluates every live group (queue mode only) so a group does
not wait on a workflow; that pass is an accelerator, not the enforcement.
Run `group-check` from a `merge_group` workflow with `statuses: write`
(Phase C, `merge-queue-ci.md`; no workflow is installed by this phase).

Still open and why the mode stays disabled: the grant store is PR comments
(not tamper-proof), the post-pass-to-merge window above, and no live pilot has
confirmed the fake's model of GitHub's group semantics.
`QUEUE_EXECUTION_ENABLED` and `INVARIANT_FULLY_DEMONSTRATED` stay `false`.
