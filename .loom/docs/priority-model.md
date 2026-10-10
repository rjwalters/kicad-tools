# Priority model: `loom:important` / `loom:very-important` (#11103)

Loom's priority is **three levels with two labels**, applied the same way by
everyone. The operator, Guide and Champion may all add or remove both.

| Level | Label |
|---|---|
| default | *(no label)* |
| important | `loom:important` |
| very important | `loom:very-important` |

`loom:very-important` is to be capped so it cannot be flooded; the cap is
follow-up #11278 and is not enforced yet. Neither label is a
hold, and the daemon never inherits either one: if a blocker must go first,
whoever sets the priority labels the blocker too. That judgment belongs to
roles (Guide, Champion), not to dispatch.

The reference implementation is `loom-daemon/src/priority_pick.rs` (pure,
seedable, unit-tested). Rollout status is tracked on #11103.

**Wired into dispatch.** The multi-workspace work-finder tick orders its
candidates by repeated draws (`work_finder/workspace_draw.rs`): each draw
picks a workspace as below and places that workspace's next issue; the
shared concurrency budget is filled in that order. The RNG is seeded once
per tick, and the seed plus every draw are exported as
`pick.decision.workspace_draw` (and the published plan's `position` is the
draw order). The single-workspace tick uses the in-workspace order alone.
Until the old labels are migrated, a legacy bridge maps
`loom:operator-priority` to important, and `loom:operator-high-priority` and
a verified red-main fix to very important.

## Selection: workspace first, then issue

1. **Pick a workspace** among those with dispatchable work, at random,
   weighted by workspace priority.
   - A workspace holding any open, dispatchable `loom:very-important` work
     wins the draw. If several do, the draw is among just those. Otherwise it
     is over every workspace with dispatchable work.
   - **Weight mapping.** A workspace's `fleet_priority` (the `priority`
     integer in `workspaces.json`; lower = higher priority, default `100`) is
     reinterpreted as a weight: `weight = max(1, 1000 / (1 + priority))`,
     integer division. Priority `0` weighs 1000, `10` weighs 90, the default
     `100` weighs 9. A tool repo pinned to `0` is therefore drawn about 110
     times as often as a default-priority repo, but no workspace is ever
     starved. `priority_pick::weight_for_priority` is the one implementation.
   - **Reproducibility.** The draw uses a seedable RNG (`PickRng`, SplitMix64).
     A fixed seed reproduces the exact pick sequence. The draw (pool,
     candidates with weights, total weight, roll, pick) is `WorkspaceDraw`,
     the record `pick.decision` carries.
2. **Pick the issue within that workspace** by, in order: level
   (`loom:very-important`, then `loom:important`, then none); oldest
   `createdAt` first; issue number ascending.

**Removed ordering keys** (they go away as the rollout lands): the starred-at
time and its `starred_at_store`, the red-main-fix key (whoever finds a
red-main fix labels it `loom:very-important`), the inherited-level key, and
the cross-workspace tier and round-robin interleave.

## Migration

| Old | New |
|---|---|
| `loom:operator-priority` | `loom:important` |
| `loom:operator-high-priority` | `loom:very-important` |
| `priority:high` (undeclared) | `loom:important` |
| `loom:high-priority-inherited` | retired, no mapping (it was daemon-derived) |
| `tier:goal-advancing` / `tier:goal-supporting` / `tier:maintenance` | retired, **no automatic mapping**; Guide applies `loom:important` where warranted |

Label definitions live in `defaults/labels.json` (`loom-daemon labels generate
--write` regenerates both `labels.yml` copies). Relabeling live issues is a
forge mutation done by an operator with `./.loom/scripts/sync-labels.sh` after
the daemon stops reading the old labels; it is deliberately not part of the
code change.
