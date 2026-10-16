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
per tick from the minute and the host id (#11683), and the seed, its inputs
and every draw are exported as
`pick.decision.workspace_draw` (and the published plan's `position` is the
draw order). The single-workspace tick uses the in-workspace order alone.
Since #11508 the level comes only from the two labels: there is no legacy
bridge, so the retired star labels carry no dispatch weight until they are
migrated (table below).

**Priority changes order, never eligibility or admission** (operator ruling,
#11508). A priority issue is dispatched only once it is `loom:issue` (there is
no priority side listing), and no priority exempts a candidate from any gate
(caps, build back-off, file overlap).

## Selection: workspace first, then issue

> **Step dispatch (#11894, live since #11909; one pool since #12185).** The step tick no longer picks a workspace first. It draws one *item* from a single pool of every actionable item across the workspaces, seeded and weighted `w_repo × priority × age × S_r^(α−1) × (1 + in_flight_repo)^(−β)` (`loom-draw/v3`), over items at every stage, not only `loom:issue`. `w_repo` is the workspace base weight described below; the repo-level bonuses below are retired. The knobs, the formula and what each does are in [`hyperparameters.md`](hyperparameters.md#the-item-draw-12185); the design is [`docs/design/work-finder-step-dispatch.md`](https://github.com/rjwalters/loom/blob/main/docs/design/work-finder-step-dispatch.md) §1 ruling 1 and §4.2. The numbered steps below describe the legacy pass-1 queue order (`workspace_draw::order`), which the step tick does not use.

1. **Pick a workspace** among those with dispatchable work, at random, with
   probability **P = w / Σw** (#11742; operator ruling 2026-10-13). There are **no tiers**:
   nothing wins the draw outright.
   - **Base weight.** A workspace's `weight` (the integer in `workspaces.json`,
     set from the fleet store's required `repos[].weight`; higher = more draws,
     `0` = never drawn) is its share of draws. N repos at weight 1 and loom at
     weight N give loom about half the draws while every repo has work.
   - **Bonuses (retired for the step tick, #12185).** While a workspace's remaining candidates include a verified
     red-main fix, `loom:very-important` work or `loom:important` work, the
     matching bonus is added to its weight; bonuses stack:
     `w = weight + redMainFix? + veryImportant? + important?`. The points are
     formerly hyperparameters `dispatch.weightBonusRedMainFix` (default `400`),
     `dispatch.weightBonusVeryImportant` (`100`) and
     `dispatch.weightBonusImportant` (`20`); the keys are now accepted and
     ignored (see [`hyperparameters.md`](hyperparameters.md)), and this legacy
     order uses the defaults. A workspace of weight `0`
     earns no bonus: it stays off. `priority_pick::draw_workspace` is the one
     implementation.
   - **Reproducibility.** The draw uses a seedable RNG (`PickRng`, SplitMix64).
     A fixed seed reproduces the exact pick sequence. The draw (candidates
     with base weight, bonus and effective weight, total weight, roll, pick)
     is `WorkspaceDraw`, the record `pick.decision` carries. The dispatch
     tick's seed is derived
     from the wall-clock minute and the host id (scheme `loom-draw/v1`, #11683:
     first 8 bytes, big-endian, of
     `sha256("loom-draw/v1:" + floor(unix_utc_secs/60) + ":" + host)`), and
     the record carries both inputs, so a logged `(minute, host)` replays the
     order. See [`daemon-reference.md`](daemon-reference.md) §"Dispatch order
     and the red-main-fix lane".
2. **Pick the issue within that workspace** by, in order: verified
   red-main fix first; level (`loom:very-important`, then `loom:important`,
   then none); oldest `createdAt` first; issue number ascending. The
   published key order is `main_red_fix → level → created_at → number`.

**The red-main-fix lane** (#11547, operator ruling on #11278). A verified
red-main fix is an issue carrying `<!-- loom:main-red-fix -->` whose repo's
`main` is verified red this tick. While `main` is red nothing in that repo can
merge, so the fix leads its workspace and adds the red-main bonus to the
workspace's weight in the draw. It is not a level and changes no gate: it keeps only its existing bypass
of the build back-off and the file-overlap gate,
which a red `main` would otherwise deadlock. On a green `main` the marker
grants nothing. With `autonomous.workFinder.redMainAutofile.enabled` on (off
by default), the daemon files that issue itself when `main` turns red, and
closes it when `main` is green again (#11766; daemon-reference.md,
"Auto-filed red-main fix issue").

**Removed ordering keys** (gone from dispatch since #11508): the star, the
starred-at time and its restart store, the inherited-level key, and the
cross-workspace tier and round-robin interleave; and (#11742) the draw's
red-main-fix and very-important pool tiers, the `fleet_priority` softmax and
`dispatch.drawTemperature`.

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
| `priority:high` | `loom:important` |
| `loom:high-priority-inherited` | retired, no mapping (it was daemon-derived) |
| `tier:goal-advancing` / `tier:goal-supporting` / `tier:maintenance` | retired, **no automatic mapping**; Guide applies `loom:important` where warranted |

This table is data: the top-level `retired` array of `defaults/labels.json`
(#11513), one `{name, replaced_by, owned, retired_in}` entry per old label
(`replaced_by: null` = remove, no replacement). It also lists the #11105
retirements (`loom:urgent`, `loom:heavy`, `loom:operator-objective`). It is
never rendered into `labels.yml`, so the additive `sync-labels.sh` cannot
re-create a retired label. `owned: false` (a label Loom never declared)
means the migration adds the replacement but never removes or deletes the old
label; `priority:high` is `owned: true` (#11675). The registry change itself
mutates no forge.

### Running the forge-side migration (#11511)

`loom-daemon labels migrate` reads this array and relabels each repo's **open**
issues and PRs (closed items are never touched). It is a dry run unless
`--apply` is passed:

```bash
loom-daemon labels migrate --all-workspaces          # preview every managed repo
loom-daemon labels migrate --all-workspaces --apply  # then migrate
loom-daemon labels migrate --repo OWNER/NAME [--apply]   # or one repo
.loom/scripts/sync-labels.sh --migrate-retired [--dry-run]  # sync first, then migrate
```

Per open item it adds `replaced_by` before removing the old label. It then
re-reads the label's open usage and deletes the label only when nothing open
still carries it; a failed read keeps it (`KEPT`). An `owned: false` label is
add-only: never removed from an item, never deleted. A replacement missing on
the forge is `SKIPPED`, never created; run `sync-labels.sh` first. A second
`--apply` makes no writes. `resync-installed.sh` only ever runs the dry run and
prints the apply command when anything is pending.
