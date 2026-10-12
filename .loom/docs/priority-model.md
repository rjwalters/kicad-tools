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
bridge, so the retired star labels carry no dispatch weight until they are
migrated (table below).

**Priority changes order, never eligibility or admission** (operator ruling,
#11508). A priority issue is dispatched only once it is `loom:issue` (there is
no priority side listing), and no priority exempts a candidate from any gate
(caps, build back-off, file overlap, repo-sharding slice).

## Selection: workspace first, then issue

1. **Pick a workspace** among those with dispatchable work, at random,
   weighted by workspace priority.
   - The pool is three-tier (#11547). A workspace whose next candidate is a
     verified red-main fix wins the draw (`pool: "red-main-fix"`); else a
     workspace holding any open, dispatchable `loom:very-important` work wins
     (`"very-important"`); if several qualify at a tier, the draw is among
     just those. Otherwise it is over every workspace with dispatchable work
     (`"all"`).
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
2. **Pick the issue within that workspace** by, in order: verified
   red-main fix first; level (`loom:very-important`, then `loom:important`,
   then none); oldest `createdAt` first; issue number ascending. The
   published key order is `main_red_fix → level → created_at → number`.

**The red-main-fix lane** (#11547, operator ruling on #11278). A verified
red-main fix is an issue carrying `<!-- loom:main-red-fix -->` whose repo's
`main` is verified red this tick. While `main` is red nothing in that repo can
merge, so the fix leads: first in its workspace and its workspace first in the
draw. It is not a level and changes no gate: it keeps only its existing bypass
of the build back-off, the file-overlap gate and the repo-sharding slice,
which a red `main` would otherwise deadlock. On a green `main` the marker
grants nothing.

**Removed ordering keys** (gone from dispatch since #11508): the star, the
starred-at time and its restart store, the inherited-level key, and the
cross-workspace tier and round-robin interleave.

## The very-important cap (#11509)

At any time, a workspace has at most one very-important issue:
`priority_pick::VERY_IMPORTANT_CAP = 1` (a constant, not a config knob;
operator ruling on #11278).

- **Which one.** The oldest **eligible** `loom:very-important` issue of the
  workspace, by `createdAt` then issue number (`very_important_holders`).
  Eligible means it carries `loom:issue` or `loom:building` and no hold label
  (the label registry's `hold` property, `defaults/labels.json`; operator
  ruling round 3 on #11278, #11559). A building issue keeps the slot, so a
  second one is not promoted while the first is in flight; an open PR is
  covered by that same `loom:building`, which stays until the closing merge
  (#6199). A blocked, operator-held or not-yet-approved very-important issue
  keeps its label but does not hold the slot while it cannot move. Once a
  holder exists, every other very-important issue in the ready listing ranks
  as `loom:important`.
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

This table is data: the top-level `retired` array of `defaults/labels.json`
(#11513), one `{name, replaced_by, owned, retired_in}` entry per old label
(`replaced_by: null` = remove, no replacement). It also lists the #11105
retirements (`loom:urgent`, `loom:heavy`, `loom:operator-objective`). It is
never rendered into `labels.yml`, so the additive `sync-labels.sh` cannot
re-create a retired label. `owned: false` (`priority:high`, never declared by
Loom) means the migration adds the replacement but never removes or deletes
the old label. Relabeling live issues is the forge-side migration (#11511),
which reads this array; the registry change itself mutates no forge.
