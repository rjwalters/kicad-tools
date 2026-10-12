# Priority model: `loom:important` / `loom:very-important` (#11103)

Loom's priority is **three levels with two labels**, applied the same way by
everyone. The operator, Guide and Champion may all add or remove both.

| Level | Label |
|---|---|
| default | *(no label)* |
| important | `loom:important` |
| very important | `loom:very-important` |

`loom:very-important` is capped at **one per workspace** so it cannot be
flooded (see "The very-important cap" below); `loom:important` is uncapped.
Neither label is a
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
Since #11508 the level comes only from the two labels: there is no legacy
bridge, so `loom:operator-priority` / `loom:operator-high-priority` carry no
dispatch weight until they are migrated (table below).

**Priority changes order, never eligibility or admission** (operator ruling,
#11508). A priority issue is dispatched only once it is `loom:issue` (there is
no priority side listing), and no priority exempts a candidate from any gate
(caps, build back-off, file overlap, repo-sharding slice).

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

**Removed ordering keys** (gone from dispatch since #11508): the star, the
starred-at time and its restart store, the red-main-fix key, the
inherited-level key, and the cross-workspace tier and round-robin interleave.
A verified red-main fix is not a priority: it gains no place in the order, but
keeps its bypass of the build back-off, the file-overlap gate and the
repo-sharding slice, which a red `main` would otherwise deadlock.

## The very-important cap (#11509)

At any time, a workspace has at most one very-important issue:
`priority_pick::VERY_IMPORTANT_CAP = 1` (a constant, not a config knob;
operator ruling on #11278).

- **Which one.** The oldest **open** `loom:very-important` issue of the
  workspace, by `createdAt` then issue number (`very_important_holders`). Open
  means any state: an issue being built (`loom:building`) or still in curation
  keeps the slot, so a second one is not promoted while the first is in
  flight. Every other open very-important issue ranks as `loom:important`.
- **Where.** Labels cannot be refused when applied, so the cap is enforced at
  dispatch, each tick, before the level is read (`work_finder/vi_cap.rs`).
  The work finder reads the workspace's open very-important listing only
  when its ready listing holds one; if that read fails it caps the ready
  listing alone. Forge labels are never changed.
- **Who.** No actor information is an input: the cap binds the operator,
  Guide and Champion alike.
- **Seen.** Each demotion is logged as a warning (once per issue per
  process) and exported as `pick.decision.very_important_demoted`
  (`repo`, `number`, `holder`). The ready queue shows the demoted issue at
  the `important` level.

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
