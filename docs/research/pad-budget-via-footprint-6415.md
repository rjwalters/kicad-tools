# Pad-budget via-footprint charging on board 05 (Issue #6415)

**Verdict: not landed.** A tip-weighted via-footprint charge does not close
VM U2.11 on board 05 and routes one signal net fewer than the centre-cell
charge. U2.9..U2.12 cannot all escape on F.Cu with a cost-only change.

## Background

The C++ A* charges `PadChannelBudget` (#3143 channel budgets, #6382 pad lanes)
at one cell per via, the via centre. On board 05, PHASE_C's 0.8 mm via at
(132.79, 102.345) sits 0.06 mm past U2.11's lane end and so pays nothing,
while its disc seals the VM pour link at U2.11. Earlier attempts (#6422,
#6426, closed unmerged) charged every budget the via disc touches uniformly;
no scale improved board 05, because every feasible via site on PHASE_C's 45°
F.Cu diagonal lies inside the 2 mm lane band of U2.10..U2.12 and the cost only
moved the blocking via onto U2.10's tip, where the pad-access guard vetoes it.

## Experiment

The charge was rebuilt with **tip weighting**: `PadChannelBudget` gained
`tip_axis` / `tip_pos` / `tip_dir` (filled from the lane's `axis` and
`direction`), cells weighted 2.0 at the pad tip falling linearly to 0.25 at the
far lane end, per-budget cap `2 * long_side` cells, charged once per budget
over the via disc at both via-expansion sites, on every layer. Native tests
(disc outside centre pays, grazing < sealing, tip end > far end, soft,
own-net exempt) passed. `ROUTER_CPP_BUILD_VERSION` 56 -> 57 in the experiment.

## Matched pair

Board 05, grid engine, 4 layers, `--no-auto-layers --deterministic-budget
--per-net-iterations 1000000 --no-placement-delta-feedback --seed 42
--no-cache --timeout 4500`, main `ccfc9726`-lineage (`d43f454b`), same host,
both runs concurrent (load 1-3). One build; the "main" arm is
`KCT_PAD_BUDGET_VIA_SCALE=0`, which disables the footprint charge and leaves
the centre-cell path (the byte-identity of that arm to a stock main build was
established in #6422 and not re-checked here).

| Arm | Routed signal nets | Unfinished (net-status) | VM U2.11 |
|---|---|---|---|
| main (scale 0) | 30/33 | GND_SENSE, PWM_B, nTRIP, ... | not listed as unrouted |
| tip-weighted (scale 1) | 29/33 | PWM_B, VM, nFAULT, ... | **stranded** |

Gate result: routed nets 29 < 30 (fails 1) and VM U2.11 not connected (fails
2). Criteria 3 and 4 were not evaluated; the gate had already failed and the
issue forbids a second tuning iteration.

## Conclusion

Cost shaping alone cannot reorder the via sites on PHASE_C's corridor:
sealing a tip is made expensive, so the router seals something else (here
different nets strand: VM and nFAULT instead of GND_SENSE and nTRIP). The
fix must change PHASE_C's escape (layer or approach) or VM's pour link, not
the via cost. Router code is unchanged by this issue.
