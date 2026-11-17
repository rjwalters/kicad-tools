# Does routing an expanded placement and shrinking it back help? (measured: not on the boards that need it)

Issue #6304. The proposal: place the board, scale the placement ~2x so the
router has room, route, then shrink back ~1% at a time on alternating axes,
repairing traces at each step. It is attractive as an *anytime* algorithm — a
fully-routed, DRC-clean board in hand at every step, with only area given up.

**Verdict: no-go on building the shrink loop (Phases 3–4). It is not built.**
The idea rests on one premise — *a board that is partial at 1x becomes
complete when expanded* — and on the boards that are actually partial, the
premise fails:

- **Neither board that is partial at 1x improved when expanded; each got worse
  or stayed level.** (These #6304 rows are unverified since #6314; see the
  marked subsection and the #6312 re-measurement of board 05 below.)
  Unfinished signal nets, 1x → 1.5x, under two protocols:
  board 05 went 4 → 6 and 9 → 9; chorus-test 29 → 31 and 30 → 34. The 4 → 6
  and 29 → 31 pairs are confounded by host load (the expanded arm ran on a far
  busier host), so they overstate the harm; the one pair run at equal load is
  the 9 → 9. These are single runs, in `uniform` mode only. Neither board has
  a 2x row: the router refuses board 05 at 2x and chorus at 2x was not run.
- **Expansion costs the one resource those boards are short of.** In #6304's
  rows both ran to their time budget at 1x, and at 1.5x the router's grid-cell
  estimate is 1.5–1.6x larger. (Those rows are unverified since #6314, and
  #6312's re-runs of board 05 with feedback off do not show it budget-bound
  at 1x: the three 1x runs took 553, 786 and 606 s of 1200, and `PHASE_A`,
  classed `budget_starved` at 1x in all three, finished in 9 of the 10
  expanded rows. The one #6312 run of 1x with feedback at its default, as
  #6304 had it, did run to 1188 s.)
- **`kct route` as shipped refuses 2x boards that have a fine-pitch part.** Its
  auto-grid is cell-budgeted, so a 4x-area board gets a coarser grid, which
  fails the router's own `clearance/2` safety rule (boards 04 and 05 here;
  board 02, which has no such part, routes).
- **More room is not reliably better.** Board 04 routes 9/9 at 1x and 8/9
  uniformly spread to 2x on the same grid. On board 03's explicit grid the
  `rigid` rows are complete at 1.5x and two signal nets short at 2x, while the
  `uniform` rows on the same grid run the other way (no gain at 1.5x, the
  stuck net routed at 2x). See the caveats on that 2x row below before
  reading it as a trend.

**There is a positive, and it should not be lost in the verdict.** On board
03 with an explicit 0.05 mm grid, net `USB_D+` is unfinished at 1x and the
board is fully complete — 27/27 nets, zero errors on both DRC engines — when
its placement is expanded 1.5x with decoupling held rigid to its IC. (The
`uniform` 2x row on that grid also routes `USB_D+` with zero errors, leaving
only a discontinuous `GND` pour.) That is exactly what the proposal predicts.
It is one net on one board, in a grid configuration where the *default* grid
already routes that net at 1x, so it does not change the verdict; but it is a
real instance of the premise holding, and a single shrink of that one route is
the cheapest possible test of the rest of the idea. That probe is filed as
#6310, and it answered no-go: see
["The one shrink"](#the-one-shrink-board-03-rigid-15x-back-to-1x-issue-6310).

**`rigid` mode on board 05 has since been measured (#6312), and it does not
help, under protocol (a) at a 1200 s budget.** On board 05 with
placement-delta feedback off, 1x leaves 4 routed nets unfinished in each of
three runs; seven `rigid` runs at five scales from 1.1x to 1.5x leave 4 to 7,
and `kct check` errors rise from 4 to between 5 and 7. One run per row except
two rows run twice. Seeds are not independent samples here: in one pair of
runs, of which the seed-43 one is not committed, seeds 42 and 43 gave the same
route, while the committed seed-43 run and a same-seed repeat each differ
from the first run in copper (though not in which nets finish). So there is
no replication across seeds. No row varied the time or iteration budget, and
the two `rigid` 1.5x runs are the only feedback-off rows it cut short.
The one row level with 1x (`rigid` 1.2x) has a single run. Full tables, what
was cut, and what the result does not cover are in
["Rigid mode on the partial boards"](#rigid-mode-on-the-partial-boards-issue-6312).
chorus-test was not routed in `rigid` mode.

Phase 2 answers the question the issue flagged as the main risk: would
shrinking be geometry, or re-topology? In the eight expanded routes tabulated
below, **4 of 1,550** inter-footprint gaps carrying copper hold more than they
could at 1x, all four on chorus at 1.5x. (The untabulated runs were reported
as adding four more, on chorus's other 1.5x route, and none elsewhere; those
totals can no longer be checked — see the Phase 2 section.) Shrinking would be
almost entirely geometry. The idea fails at step 3 (route), not step 4
(shrink) — the opposite of where the risk was expected.

What the data suggests building instead is in
["Refinements the data suggests"](#refinements-the-data-suggests).

Tool: `scripts/research/expand_route_experiment.py` (tests:
`tests/test_expand_route_experiment.py`). Nothing under `src/` changed; default
routing behaviour is untouched.

## What was measured

| Phase | Question | How |
|---|---|---|
| 1 | Does the unchanged router finish more of a board when its placement is expanded? | Scale the placement by 1.0 / 1.5 / 2.0, run `kct route`, grade the output. |
| 2 | Would the expanded topology fit back at 1x? | Count inter-footprint gaps whose routed traffic needs more width than the gap has at 1x. |
| 3–4 | Shrink loop and fleet evaluation | **Not built** — see the verdict. |

### What scales and what does not

The placement is mapped affinely about the centre of the `Edge.Cuts` bounding
box. **Scaled:** footprint positions, the board outline, every other top-level
graphic, and zone / keepout outlines (stale `filled_polygon` fills are dropped
so the router refills them). **Not scaled:** anything inside a footprint — pad
pitch, pad size, courtyard, drills. A footprint moves; it does not grow.

Two placement modes:

- **`uniform`** — every footprint position is scaled independently. This also
  drags a decoupling capacitor away from the pin it serves, which is
  electrically wrong and manufactures new long nets.
- **`rigid`** — footprints are grouped into rigid clusters and each cluster
  *translates* by its centroid's displacement, so only inter-cluster distance
  grows. A cluster is an "anchor" (≥ 6 pads) plus every ≤ 2-pad part whose pad
  bounding box is within 3 mm of it **and** shares a net with it; the nearest
  such anchor wins. Sharing a net is what separates "this cap decouples that
  IC" from "this resistor happens to sit beside it" — without that condition a
  dense board collapses into one cluster and nothing expands. The rule is
  conservative, and how conservative varies a great deal by board — see the
  next section.

### How much each board clusters

`rigid` differs from `uniform` only for footprints that land in a multi-part
cluster, so these counts say how much a `rigid` row can mean on each board.
They are `rigid_clusters()` on the unrouted inputs at this branch's head
(the chorus fixture is out of tree).

| Board | Footprints | Anchors (≥ 6 pads) | Parts with ≤ 2 pads | …within 3 mm of an anchor | …and sharing a net with it | Multi-part clusters (sizes) | Footprints in them |
|---|---|---|---|---|---|---|---|
| 00 | 3 | 0 | 3 | 0 | 0 | 0 | 0 |
| 01 | 4 | 0 | 4 | 0 | 0 | 0 | 0 |
| 02 | 19 | 2 | 17 | 2 | 2 | 1 (3) | 3 |
| 03 | 38 | 5 | 27 | 12 | 12 | 4 (9, 3, 2, 2) | 16 |
| 04 | 17 | 2 | 14 | 1 | 1 | 1 (2) | 2 |
| 05 | 42 | 5 | 35 | 10 | 10 | 4 (5, 5, 2, 2) | 14 |
| chorus | 90 | 3 | 76 | 4 | 1 | 1 (2) | 2 |

Three things follow, and they qualify every statement about `rigid` below.

- **Board 03 is the only board run in `rigid` mode that has a large cluster**
  (one of nine parts; 16 of 38 footprints clustered). Every `rigid` result
  that differs from its `uniform` twin by more than three parts' positions
  comes from it.
- **On boards 02 and 04 `rigid` moves two or three footprints differently**,
  and on 00 and 01 none. Those rows are near-replicates of `uniform`, not
  tests of clustering.
- **On chorus `rigid` would differ from `uniform` in one relative position.**
  The rule forms a single two-part cluster there. A cluster translates by the
  displacement of the mean of its members' positions, so both of its
  footprints land somewhere other than where `uniform` puts them; what is
  preserved, and what differs from `uniform`, is the one offset between them.
  Only three footprints qualify as anchors and only four of the 76 small parts
  sit within 3 mm of one. A chorus `rigid` run under this rule would say
  nothing about rigid clustering; it needs a different rule first. Board 05,
  by contrast, clusters about as much as board 03 (14 of 42). Neither was run
  in `rigid` mode in #6304; see
  ["Rigid mode on the partial boards"](#rigid-mode-on-the-partial-boards-issue-6312).

Choices worth stating because they are not obviously right:

- **"Fixed" parts are not modelled.** Edge connectors and mounting holes scale
  like everything else, so a connector 2 mm from the edge is 4 mm from it at
  2x. The outline scales with them and a shrink back to 1x undoes the map
  exactly, so this does not bias Phase 1 — but an expanded board is not a
  mechanically valid board in its own right, and a real implementation would
  have to pin those parts and scale the rest around them.
- **Boards that arrive with copper are refused** (06's pre-routed diff pairs,
  07's 119 routed connections). A trace endpoint sits on a *pad*, and a pad
  keeps its offset from the footprint origin, so scaling the copper affinely
  tears it off the pad it terminates on. This is not a harness limitation to
  be engineered away: it is the reason a shrink step needs a repair pass rather
  than a coordinate transform, and it applies to every pad on the board at
  every step.

### Reproduce

```bash
uv run kct build-native --check     # C++ backend: available, build version 49

# Same explicit grid at every scale (the clean A/B).
uv run python scripts/research/expand_route_experiment.py route \
    --boards 04 --grid-policy pinned --grid-mm 0.05 \
    --scales 1.0,1.5,2.0 --modes uniform,rigid --timeout 600 \
    --work-dir /tmp/ers --json /tmp/ers/pinned_04.json
#   03: --grid-mm 0.05  --timeout 900
#   02: --grid-mm 0.051 --timeout 900 --modes uniform

# The router exactly as shipped, and with an area-scaled cell budget.
uv run python scripts/research/expand_route_experiment.py route \
    --boards 04,02,03,01,00 --grid-policy area \
    --scales 1.0,1.5,2.0 --modes uniform,rigid --timeout 600 \
    --work-dir /tmp/ers --json /tmp/ers/area.json
uv run python scripts/research/expand_route_experiment.py route \
    --boards 04,02 --grid-policy default --scales 2.0 --timeout 600 \
    --work-dir /tmp/ers --json /tmp/ers/default.json

# Boards partial at 1x, protocol (a): pinned layers + iteration-budgeted search.
uv run python scripts/research/expand_route_experiment.py route \
    --boards 05 --grid-policy area --scales 1.0,1.5 --timeout 1200 \
    --extra "--layers 4 --no-auto-layers --deterministic-budget --per-net-iterations 1000000" \
    --work-dir /tmp/ers --json /tmp/ers/det_05.json
uv run python scripts/research/expand_route_experiment.py route \
    --boards chorus --grid-policy area --scales 1.0,1.5 --timeout 1200 \
    --work-dir /tmp/ers --json /tmp/ers/det_chorus.json
#   protocol (b): --boards 05 --scales 1.0,1.5,2.0 (chorus: 1.0,1.5)
#                 --grid-policy area --timeout 600, no --extra

# Phase 2, from any of the JSON files above.
uv run python scripts/research/expand_route_experiment.py gaps \
    --from-json /tmp/ers/det_chorus.json
```

The chorus fixture lives in a sibling repository; the harness finds it next to
the primary checkout, or takes `--chorus-pcb`. Its recipe is
`scripts/route_chorus.py`'s main-pass flags minus placement feedback (which
moves parts, confounding a placement-scale A/B) and minus the completion and
rescue stages (which are not `kct route`).

## Measurement conditions (read before trusting any number)

- Commit `dd2947c7` + this branch, C++ backend built and active (`kct
  build-native --check`: build version 49), macOS, **8 logical CPUs**, KiCad
  10.0.6.
- **The host was shared and at times badly oversubscribed**: load average
  ranged 3 → 34 on 8 CPUs while other sweep builders compiled. Per-row
  `loadavg_end` is in the harness JSON. Wall-clock seconds are indicative
  only. Where load could have decided a comparison it is called out next to
  the table.
- `--seed 42` and `PYTHONHASHSEED=0` in every run. **One run per row.** A fixed
  seed did not guarantee an identical route on re-run; see "Run-to-run
  variance" under [Not measured](#not-measured).
- **The harness result JSON and routed boards were kept in a scratch directory,
  were not committed, and no longer exist.** (This is #6304's data. #6312's
  result JSON is committed under `data/research/expand_route_shrink/`.) Every number in this note was
  transcribed from them at the time. Numbers that appear in a table can be
  cross-checked against other rows; the few that were only ever totals over
  untabulated rows cannot, and are marked where they occur.
- Grading is the same referee `krt_compare.py` uses
  (`kicad_tools.benchmark.external.metrics`): strict-geometry completion, both
  DRC engines (`kct check` in-process and `kicad-cli pcb drc --refill-zones`),
  and `router/stuck_classifier.py` for the per-net stuck class.
- **"Signal nets unfinished" is the column to read.** It is the classifier's
  unfinished nets minus `pour_discontinuous` ones. Connection totals are
  dominated by pour connectivity (GND alone is most of a board's connections),
  which swings by tens of connections with zone-fill details and says nothing
  about signal routing: board 05 reads 127/142 and 85/142 on two runs that each
  left exactly 9 signal nets unfinished.
- The router's own tally is shown beside the referee's because they disagree on
  partial boards (board 05 at 1x, protocol (b): the router reports 18/33 routed
  where the referee finds 24 of those 33 complete). Neither is adjusted.
- `rc` is `kct route`'s exit code: 0 complete, 2 incomplete, 4 incomplete with
  DRC violations, 1 refused before routing, 124 hard deadline.

## Four measurement traps found first

As in the two experiments before this one
([net ordering](kct-route-net-order-experiment.md),
[rip-up](kct-route-ripup-experiment.md)), the first runs measured something
other than the question. All four are handled in the harness and pinned by
tests; the first two are also filed, because they are product gaps and not
just harness hazards.

1. **Grid pitch changes with board size, so a naive A/B measures the grid.**
   The auto-selected grid is cell-budgeted (`--max-cells`, default 500,000).
   Scale the board and the same budget buys a coarser pitch: board 03 routes at
   0.05 mm at 1x, 0.065 mm at 1.5x and 0.127 mm at 2x. On any board with a
   fine-pitch part the coarser grid fails the router's `grid ≤ clearance/2`
   safety rule and `kct route` **exits 1 in under two seconds without routing**
   (boards 04 and 05 at 2x). Scaling the budget with area
   (`--max-cells 500000·s²`) is the obvious fix and is not enough — the
   selector still picks different pitches at different scales, non-monotonically
   (board 00: 0.127 → 0.051 → 0.065 mm), and board 05 at 2x is still refused at
   2,000,000 cells. Only an explicit `--grid` holds the pitch constant. The
   harness has all three policies (`pinned` / `area` / `default`) and every
   table below says which was used. Filed as #6306.
2. **A hard-deadline exit leaves an artifact that looks like a result and is
   not one.** When `--timeout` fires, `kct route` exits 124 without writing the
   requested output. What it leaves depends on which stage the deadline landed
   in, which is load-dependent: board 05 at 1x on an explicit grid scored 20
   unfinished signal nets from a 600 s deadline artifact and 10 from a 900 s
   one, against 9 from a 600 s auto-grid run that finished. Worse, when the
   deadline lands inside the routing stage, the timeout sidecar's
   `unverified_output` names a board with **no copper at all** (chorus: 0
   segments, against 21,888 raw segments and 108 vias in the
   `_partial.kicad_pcb` beside it). The harness grades the `_partial` in
   preference and labels every such row; the real fix was to re-run boards 05
   and chorus under a protocol that finishes. Filed as #6308.
3. **`kct route` re-emits footprints in a different order than it read them.**
   The first Phase 2 run matched footprints by document index, paired each 1x
   footprint with an unrelated routed one, and reported 14 over-capacity gaps
   on board 03. It was caught by the control this metric must pass: a DRC-clean
   1x board measured against *itself* has to score zero. Gaps are now matched
   by reference and that control is a test.
4. **The boards the issue names as partial mostly are not, any more.** The
   issue (and `kicad-routing-tools-comparison.md`) cite boards 02, 03 and 04 as
   partial under plain `kct route`. On today's `main`, with default flags, all
   three finish every signal net at 1x. Only board 05 and chorus-test are
   partial under the shipped router, so they are the two boards the proposal
   has to help. The rest are controls — with the exception found on board 03
   under an explicit grid, below.

## Phase 1 results

### Pinned grid — the clean A/B

Same explicit `--grid` at every scale, 1x included, so rows differ in
placement scale alone.

| Board | Mode | Scale | Outline mm | Nets | Signal nets unfinished | Router's own tally | Connections | kct check err | kicad-cli DRC err | Vias | Wire mm | Grid mm | rc | Wall s | Stuck classes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 04 | uniform | 1 | 60x40 | 11/12 | 0 | 9/9 | 39/43 | 0 | 0 | 34 | 207.3 | 0.05 | 0 | 30.3 | pour_discontinuous 1 |
| 04 | uniform | 1.5 | 90x60 | 11/12 | 0 | 9/9 | 39/43 | 0 | 0 | 32 | 294.9 | 0.05 | 0 | 39.1 | pour_discontinuous 1 |
| 04 | uniform | 2 | 120x80 | 10/12 | **1** | 8/9 | 38/43 | 1 | 0 | 29 | 335.4 | 0.05 | 2 | 69.8 | congestion_saturated 1, pour_discontinuous 1 |
| 04 | rigid | 1.5 | 90x60 | 11/12 | 0 | 9/9 | 39/43 | 0 | 0 | 32 | 308.9 | 0.05 | 0 | 73.5 | pour_discontinuous 1 |
| 04 | rigid | 2 | 120x80 | 11/12 | 0 | 9/9 | 39/43 | 0 | 0 | 30 | 408.4 | 0.05 | 0 | 80.4 | pour_discontinuous 1 |
| 03 | uniform | 1 | 80x60 | 26/27 | **1** | 23/24 | 112/115 | 1 | 0 | 70 | 675.9 | 0.05 | 0 | 239.9 | placement_bound 1 |
| 03 | uniform | 1.5 | 120x90 | 26/27 | **1** | 23/24 | 112/115 | 1 | 0 | 63 | 933.7 | 0.05 | 0 | 413.3 | placement_bound 1 |
| 03 | uniform | 2 | 160x120 | 26/27 | 0 | 24/24 | 114/115 | 0 | 0 | 69 | 1300.8 | 0.05 | 0 | 390.6 | pour_discontinuous 1 |
| 03 | rigid | 1.5 | 120x90 | **27/27** | **0** | 24/24 | 115/115 | 0 | 0 | 67 | 957.2 | 0.05 | 0 | 285.8 | -- |
| 03 | rigid | 2 | 160x120 | 23/27 | **2** | 22/24 | 90/115 | 2750 | 0 | 35 | 1061.7 | 0.05 | 4 | 861.9 | placement_bound 2, pour_discontinuous 2 |
| 02 | uniform | 1 | 50x55 | 12/12 | 0 | 10/10 | 36/36 | 0 | 0 | 30 | 397.8 | 0.051 | 0 | 98.7 | -- |
| 02 | uniform | 1.5 | 75x82.5 | 12/12 | 0 | 10/10 | 36/36 | 0 | 0 | 32 | 582.3 | 0.051 | 0 | 135.0 | -- |
| 02 | uniform | 2 | 100x110 | 12/12 | 0 | 10/10 | 36/36 | 0 | 0 | 29 | 772.2 | 0.051 | 0 | 121.9 | -- |

Three things happen in this table, and they point in three directions.

- **Board 03 is the premise holding.** `USB_D+` (`placement_bound`) is
  unfinished at 1x and at uniform 1.5x, and routes at uniform 2x and at rigid
  1.5x. The rigid 1.5x board is fully complete: every net including the pours,
  no errors from either DRC engine. (Board 03 reaches 27/27 in three rows of
  this experiment, and all three are `rigid`.)
  The caveat is the grid. An explicit `--grid` is not equivalent to the auto
  grid at the same coarse pitch — the auto path adds finer zones around dense
  parts — and on the default grid (next table) `USB_D+` already routes at 1x.
  So expansion recovered a net that a better grid also recovers, for free.
- **Board 03 is also the premise failing, one row down.** Rigid 2x leaves two
  signal nets unfinished and 2,750 `kct check` errors (2,605 of them
  `clearance_segment_zone`). It used 862 s of its 900 s budget — a uniform
  0.05 mm grid over 160 x 120 mm is four times the cells — and its pour
  completion was cut off by the deadline. So in `rigid` mode, on this grid,
  the gain seen at 1.5x is not there at 2x. That sentence should not be
  stretched further, for three reasons in the tables themselves: the `uniform`
  rows on the same grid run the other way (no gain at 1.5x, `USB_D+` routed
  with zero errors at 2x, in 391 s); under the `area` policy board 03 at rigid
  2x is 27/27 with zero errors (next table); and this is one run that ended
  within 40 s of its deadline, so it is at least partly a budget result.
- **Board 04 regresses.** At uniform 2x it loses net `SWO`. The log shows the
  net was routed, collided with `NRST`, failed recovery, and was demoted to
  unrouted — on a board with four times the free area. The `area`-policy run
  loses the same net; the `rigid` 2x board, which differs only in keeping one
  capacitor with its IC, routes it. Filed as #6307.

The router's result is not monotone in the room it is given, in either
direction, and a one-part difference in how the room is handed out can decide
it. That alone is a reason not to build a loop that assumes each step down is
a small perturbation of the last.

### The router as shipped, and with an area-scaled cell budget

`default` = no extra flags. `area` = `--max-cells 500000·s²`. The grid column
is what the auto-selector actually chose; read every number in this table
against it.

| Board | Policy | Mode | Scale | Outline mm | Nets | Signal nets unfinished | Router's own tally | kct check err | kicad-cli DRC err | Vias | Wire mm | Grid mm | rc | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 04 | default | uniform | 2 | 120x80 | **refused** (grid 0.127 > clearance/2) | -- | -- | -- | -- | -- | -- | 0.127 | 1 | 0.7 |
| 02 | default | uniform | 2 | 100x110 | 12/12 | 0 | 10/10 | 0 | 0 | 24 | 765.4 | 0.127 | 0 | 24.9 |
| 04 | area | uniform | 1 | 60x40 | 11/12 | 0 | 9/9 | 0 | 0 | 34 | 207.3 | 0.05 | 0 | 25.6 |
| 04 | area | uniform | 1.5 | 90x60 | 11/12 | 0 | 9/9 | 0 | 0 | 32 | 294.9 | 0.05 | 0 | 28.4 |
| 04 | area | uniform | 2 | 120x80 | 10/12 | 1 | 8/9 | 1 | 0 | 29 | 335.4 | 0.05 | 2 | 61.7 |
| 04 | area | rigid | 1.5 | 90x60 | 11/12 | 0 | 9/9 | 0 | 0 | 32 | 308.9 | 0.05 | 0 | 32.8 |
| 04 | area | rigid | 2 | 120x80 | 11/12 | 0 | 9/9 | 0 | 0 | 30 | 408.4 | 0.05 | 0 | 38.0 |
| 02 | area | uniform | 1 | 50x55 | 12/12 | 0 | 10/10 | 0 | 0 | 33 | 399.7 | 0.051 | 0 | 88.6 |
| 02 | area | uniform | 1.5 | 75x82.5 | 12/12 | 0 | 10/10 | 0 | 0 | 28 | 581.0 | 0.051 | 0 | 93.4 |
| 02 | area | uniform | 2 | 100x110 | 12/12 | 0 | 10/10 | 0 | 0 | 23 | 763.9 | 0.1 | 0 | 34.6 |
| 02 | area | rigid | 1.5 | 75x82.5 | 12/12 | 0 | 10/10 | 0 | 0 | 24 | 575.3 | 0.05 | 0 | 77.9 |
| 02 | area | rigid | 2 | 100x110 | 12/12 | 0 | 10/10 | 0 | 0 | 27 | 759.0 | 0.1 | 0 | 34.8 |
| 03 | area | uniform | 1 | 80x60 | 26/27 | 0 | 24/24 | 0 | 0 | 74 | 696.0 | 0.05 | 0 | 166.3 |
| 03 | area | uniform | 1.5 | 120x90 | 26/27 | 0 | 24/24 | 0 | 0 | 67 | 971.8 | 0.065 | 0 | 148.6 |
| 03 | area | uniform | 2 | 160x120 | 26/27 | 0 | 24/24 | 0 | 0 | 72 | 1357.3 | 0.127 | 0 | 69.7 |
| 03 | area | rigid | 1.5 | 120x90 | **27/27** | 0 | 24/24 | 0 | 0 | 64 | 962.8 | 0.1 | 0 | 72.1 |
| 03 | area | rigid | 2 | 160x120 | **27/27** | 0 | 24/24 | 0 | 0 | 69 | 1260.3 | 0.1 | 0 | 94.4 |
| 01 | area | uniform | 1 | 30x25 | 3/3 | 0 | 3/3 | 0 | 0 | 5 | 63.5 | 0.1 | 0 | 4.1 |
| 01 | area | uniform | 1.5 | 45x37.5 | 3/3 | 0 | 3/3 | 0 | 0 | 3 | 89.7 | 0.1 | 0 | 4.4 |
| 01 | area | uniform | 2 | 60x50 | 3/3 | 0 | 3/3 | 0 | 0 | 1 | 127.3 | 0.127 | 0 | 4.4 |
| 00 | area | uniform | 1 | 25x20 | 3/3 | 0 | 1/1 | 0 | 0 | 0 | 8.7 | 0.127 | 0 | 9.0 |
| 00 | area | uniform | 1.5 | 37.5x30 | 3/3 | 0 | 1/1 | 0 | 0 | 0 | 12.9 | 0.051 | 0 | 9.4 |
| 00 | area | uniform | 2 | 50x40 | 3/3 | 0 | 1/1 | 0 | 0 | 0 | 17.1 | 0.065 | 0 | 7.9 |

(`rigid` rows for 00 and 01 are identical to `uniform` — neither board forms a
cluster — and are omitted.)

Three things in this table are not about signal-net completion:

- **Rigid expansion clears board 03's pour discontinuity.** On the default
  grid every signal net routes at every scale; what is left at 1x is `GND`,
  whose fill does not join all its pads (26/27, non-blocking). Both `rigid`
  rows are 27/27. Keeping the caps with their ICs while the clusters separate
  leaves the pour room to flow between them. That is the cleanest benefit
  expansion showed anywhere, and it is a zone-fill effect, not a routing one —
  note the grid moved too (0.05 → 0.1 mm), so it is not a clean A/B, though the
  pinned-grid rigid 1.5x row above shows the same thing at a fixed grid.
- **Where a coarser grid is allowed, the expanded board routes faster.** Board
  03 at 2x on a 0.127 mm grid takes 70 s against 166 s at 1x on 0.05 mm; board
  02 takes 25–35 s against 89 s. Same completion, zero DRC errors. Expansion
  buys *speed* on boards with no fine-pitch part, by licensing a coarser grid.
  That is a real effect, but it is not the one the proposal is after, and the
  copper it produces is on a grid that the 1x board cannot use. At a pinned
  grid the same boards get slower with scale, as expected.
- **Wirelength rises as geometry predicts; vias mostly fall.** Wirelength
  tracks the scale factor (board 02: 400 → 581 → 764 mm, i.e. 1.45x and 1.91x).
  Vias drop on the auto grid (02: 33 → 23; 01: 5 → 1) but not at a pinned grid
  (02: 30 → 32 → 29), so some of that is the coarser grid too.

### Boards that are partial at 1x

> **The rows in this subsection are unverified (added for #6312).** Every
> board-05 and chorus row below was run with placement-delta feedback at its
> default, and ran to or near its time budget. #6314 (fixed in PR #6323, on
> `main` from `ee1d255f`) found that a deadline during the feedback loop could
> save one trial's copper against the original placement; #6312's update
> reports a protocol-(a) run of board 05 at 1x graded at 18 unfinished signal
> nets from such a file, while the route it held before the loop had 5
> unrouted nets by the router's own count (not re-measured here). Whether any row here went through that path was never checked,
> and the boards no longer exist. **None of these numbers is used as a
> baseline anywhere below**: the 1x rows were re-run with feedback off in
> ["Rigid mode on the partial boards"](#rigid-mode-on-the-partial-boards-issue-6312).
> The rows are kept as a record of what #6304 reported, not as results. The
> same caveat applies wherever they are quoted: the summary at the top of this
> note, "By stuck class", the Phase 2 rows for 05 and chorus, "Go / no-go",
> reason 3 of "Why it mostly does not work", and the "Anything above 1200 s"
> and "Run-to-run variance" entries under "Not measured". Where #6312's
> committed rows contradict one of those passages, and not merely leave it
> unverified, the passage says so in place: that is the case for the claims
> that board 05 is budget-bound at 1x and that no `BUDGET_STARVED` net
> completes when expanded.
> Two further problems found in #6312 apply to them as well: the "signal nets
> unfinished" column leaves out `VM` on board 05 whenever the classifier calls
> it a pour net (it has no zone), and "grid-matched" below means the coarse
> pitch only (see [the traps](#four-more-measurement-traps-issue-6312)).

Two protocols that finish, and one that was abandoned.

**(a) Pinned layers, iteration-budgeted search, 1200 s.** `--layers 4
--no-auto-layers --deterministic-budget --per-net-iterations 1000000` (chorus's
recipe already has these). Each net gets the same search effort at every scale,
and layer escalation cannot split the budget. The auto grid chose the same
coarse pitch at both scales, so these pairs are grid-matched.

| Board | Scale | Outline mm | Nets | Signal nets unfinished | Router's own tally | Connections | kct check err | kicad-cli DRC err | Vias | Wire mm | Grid mm | Grid cells (est.) | rc | Wall s | Load avg |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 05 | 1 | 70x90 | 32/37 | **4** | 29/33 | 134/142 | 5 | 0 | 121 | 1110.8 | 0.065 | 3.93 M | 2 | 1183.7 | 6.6 |
| 05 | 1.5 | 105x135 | 28/37 | **6** | 27/33 | 130/142 | 7 | 0 | 95 | 1380.0 | 0.065 | 5.79 M | 4 | 1188.2 | 34.2 |
| chorus | 1 | 63x63 | 46/80 | **29** | 22/37 | 42/209 | 397 | 134 | 135 | 1482.2 | 0.051 | 3.09 M | 2 | 1193.9 | 5.6 |
| chorus | 1.5 | 94.5x94.5 | 44/80 | **31** | (deadline) | 40/209 | 335 | 184 | 96 | 2006.9 | 0.051 | 4.98 M | 124 | 1205.5 | 14.2 |

All four runs ran to the 1200 s budget. **Read the load column**: both
expanded runs happened to run while the host was far more heavily loaded than
their 1x twins. The per-net search budget is in iterations and so is
load-independent, but the 1200 s ceiling is wall-clock, and a loaded host gets
through fewer negotiation passes under it. So protocol (a) on its own
overstates how much expansion hurts. The chorus 1.5x row is also a
hard-deadline artifact, graded from the `_partial` board: its unfinished-net
count is comparable to the 1x row's, its DRC counts are not.

**(b) Router as shipped, 600 s** — auto layers, wall-clock search budget, auto
grid (0.065 mm at both 1x and 1.5x on board 05, so that pair is grid-matched
too):

| Board | Scale | Nets | Signal nets unfinished | Router's own tally | Connections | kct check err | kicad-cli DRC err | Vias | Wire mm | Grid mm | rc | Wall s | Load avg |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 05 | 1 | 27/37 | **9** | 18/33 | 127/142 | 66 | 72 | 95 | 842.1 | 0.065 | 4 | 579.4 | 6.3 |
| 05 | 1.5 | 26/37 | **9** | 17/33 | 85/142 | 66 | 78 | 66 | 1187.0 | 0.065 | 4 | 584.8 | 5.0 |
| 05 | 2 | **refused** (grid 0.127 > clearance/2, even at `--max-cells 2000000`) | -- | -- | -- | -- | -- | -- | -- | 0.127 | 1 | 1.6 | 5.3 |
| chorus | 1 | 45/80 | **30** | (deadline) | 38/209 | 386 | 178 | 108 | 1317.9 | 0.051 | 124 | 605.4 | 23.5 |
| chorus | 1.5 | 41/80 | **34** | (deadline) | 36/209 | 339 | 171 | 72 | 1764.8 | 0.051 | 124 | 605.7 | 4.7 |

Here the load runs the other way. Board 05's pair ran at equal load and came
out equal: 9 and 9. Chorus's *1x* run had the loaded host, and the expanded
board still finished four fewer nets. Both chorus rows are deadline artifacts.

Put the two protocols together and the reading is: **at best parity (05, equal
load), otherwise worse, and never better** — on four 1x/1.5x pairs. Those
four are two boards under two protocols with one seed and one run each, not
four independent samples, and three of the four chorus rows are deadline
artifacts. The premise needed a large improvement to be worth a shrink loop,
and there is no improvement.
(Host load favoured the 1x arm in two of the four pairs, the expanded arm in
one, and neither in one.)

**(c) Explicit `--grid`, 900 s** — abandoned. Both boards' 1x runs died at the
hard deadline in a late stage (05: 10 unfinished; chorus: 48), the sweep was
stopped, and the numbers are noise for the reason given in trap 2. Mentioned so
nobody repeats the protocol; not evidence either way.

**Not measured in #6304** (board 05 in `rigid` mode has since been run, see
["Rigid mode on the partial boards"](#rigid-mode-on-the-partial-boards-issue-6312))**:**
board 05 and chorus at 2x (the router refuses 05 under
`default` and `area`, and the only protocol that could route it — explicit
grid — could not finish even the 1x board in budget; chorus at 2x was not
run); and either partial board in `rigid` mode. Given what `rigid` did for
board 03 at 1.5x, the second is the more important omission, and it is tracked
as #6312. It is not the same size of omission on the two boards: board 05
forms four multi-part clusters covering 14 of its 42 footprints, so a `rigid`
run there is a real test; chorus forms one two-part cluster out of 90, so a
`rigid` run there would differ from a `uniform` run in one relative position
(["How much each board clusters"](#how-much-each-board-clusters)).

A side observation on chorus's DRC columns: the fixture is DRC-dirty **before
any copper is added**. The copper-less snapshot from trap 2, graded once by
hand, scores 126 `kicad-cli` errors (45 `shorting_items`, 45
`solder_mask_bridge`, 18 `courtyards_overlap`, 12 `clearance`) and 382 `kct
check` errors at 1x, and 88 / 340 at 1.5x — spreading the parts clears 17 of
the 18 courtyard overlaps and the clearance errors, and leaves the shorting
and mask-bridge errors almost where they were (45 → 42). So
chorus's DRC counts here mostly measure the placement and footprints, not the
routing. Not investigated further.

### By stuck class

The issue predicted that `ESCAPE_BLOCKED` nets would not benefit (pin pitch
does not scale) and `PLACEMENT_BOUND` nets would. **The first half could not be
tested: no net on any board was classed `ESCAPE_BLOCKED` at 1x.** The second
half held on one board and failed on the two that matter.

Each net keeps the class it had at 1x and is then looked up in the expanded
run.

| Board | Protocol | Mode / scale | 1x stuck class | Stuck at 1x | Completed when expanded | Still stuck | Newly stuck when expanded (complete at 1x) |
|---|---|---|---|---|---|---|---|
| 03 | pinned | uniform 1.5 | placement_bound | 1 | 0 | 1 | 0 |
| 03 | pinned | uniform 2 | placement_bound | 1 | 1 | 0 | 0 |
| 03 | pinned | rigid 1.5 | placement_bound | 1 | 1 | 0 | 0 |
| 03 | pinned | rigid 2 | placement_bound | 1 | 1 | 0 | placement_bound 2 |
| 05 | (a) | uniform 1.5 | budget_starved | 3 | 0 | 3 | placement_bound 2 |
| 05 | (a) | uniform 1.5 | placement_bound | 1 | 0 | 1 | |
| chorus | (a) | uniform 1.5 | budget_starved | 8 | 0 | 8 | placement_bound 7, congestion_saturated 1 |
| chorus | (a) | uniform 1.5 | congestion_saturated | 7 | 2 | 5 | |
| chorus | (a) | uniform 1.5 | placement_bound | 14 | 4 | 10 | |
| 05 | (b) | uniform 1.5 | budget_starved | 4 | 0 | 4 | congestion_saturated 1 |
| 05 | (b) | uniform 1.5 | congestion_saturated | 1 | 0 | 1 | |
| 05 | (b) | uniform 1.5 | placement_bound | 4 | 1 | 3 | |
| chorus | (b) | uniform 1.5 | budget_starved | 8 | 0 | 8 | placement_bound 5, budget_starved 2, congestion_saturated 1 |
| chorus | (b) | uniform 1.5 | placement_bound | 22 | 4 | 18 | |

(Pour nets omitted.)

On the two partial boards:

- `PLACEMENT_BOUND`: 4 of 15 completed under (a), 5 of 26 under (b) — about
  one in four or five. Meanwhile 9 nets (a) and 5 nets (b) that were complete
  at 1x became `PLACEMENT_BOUND` when expanded.
- `BUDGET_STARVED`: 0 of 11 under (a), 0 of 12 under (b). #6304 called this
  expected — these nets are short of search budget, and a larger board makes
  every search longer. **#6312's committed rows do not repeat it on board
  05:** with feedback off, three nets are `budget_starved` at 1x in each of
  three runs, and one of them, `PHASE_A`, finished in 9 of the 10 expanded
  rows. (The other two, `PHASE_B` and `PHASE_C`, finished in none; see
  #6315.)
- `CONGESTION_SATURATED`: 2 of 7 under (a), 0 of 1 under (b).

So expansion does move individual nets — under (a), six completed that were
stuck at 1x — but ten that were complete came unstuck, and the class label
does not predict which. On board 03 the single `PLACEMENT_BOUND` net behaves
exactly as predicted. The difference between that board and the other two is
the subject of the next two sections.

## Phase 2: would the expanded topology fit at 1x?

For every pair of footprints that share a copper layer and have a clear line
of sight at 1x — the shortest segment joining their pad bounding boxes hits no
third footprint on that layer — take the same pair on the routed board, draw
the shortest segment between their pad boxes *there*, and collect the routed
segments on the shared layer that cross it, one trace per net. The gap is
**over 1x capacity** when the width that bundle needs (trace widths, plus one
clearance each side and between neighbours) exceeds the gap's width at 1x.
"Through traffic alone" drops nets that have a pad on either footprint, since
a trace ending on one of the two obstacles is using its own escape lane rather
than transiting the gap; it is the conservative count.

| Board | Protocol | Mode / scale | Signal nets unfinished | Gaps examined | Carrying copper | Over 1x capacity | …on through traffic alone | Tightest loaded gap (1x width / needed / traces) |
|---|---|---|---|---|---|---|---|---|
| 04 | pinned | 1x | 0 | 80 | 33 | 0 | 0 | 2.0 / 0.5 mm / 1 |
| 04 | pinned | uniform 2 | 1 | 80 | 31 | 0 | 0 | 1.35 / 0.5 mm / 1 |
| 04 | pinned | rigid 2 | 0 | 80 | 37 | 0 | 0 | 1.35 / 0.5 mm / 1 |
| 02 | pinned | 1x | 0 | 103 | 59 | 0 | 0 | 1.69 / 0.5 mm / 1 |
| 02 | pinned | uniform 2 | 0 | 103 | 74 | 0 | 0 | 1.69 / 0.5 mm / 1 |
| 03 | pinned | 1x | 1 | 227 | 155 | 0 | 0 | 1.05 / 0.5 mm / 1 |
| 03 | pinned | uniform 2 | 0 | 227 | 150 | 0 | 0 | 0.55 / 0.5 mm / 1 |
| 03 | pinned | **rigid 1.5** | **0** | 227 | 154 | **0** | **0** | 0.55 / 0.5 mm / 1 |
| 03 | pinned | rigid 2 | 2 | 227 | 136 | 0 | 0 | 1.05 / 0.5 mm / 1 |
| 05 | (a) | 1x | 4 | 422 | 283 | 0 | 0 | 1.9 / 0.5 mm / 1 |
| 05 | (a) | uniform 1.5 | 6 | 422 | 280 | 0 | 0 | 1.6 / 0.5 mm / 1 |
| chorus | (a) | 1x | 29 | 860 | 680 | 0 | 0 | 1.56 / 0.85 mm / 2 |
| chorus | (a) | uniform 1.5 | 31 | 860 | 688 | **4** | **2** | 0.18 / 0.5 mm / 1 |

The eight expanded rows above carry copper in 1,550 gaps, of which 4 are over
1x capacity — all on the one chorus row.

Rows not shown — every other scale and mode, the `area`-policy runs, and
protocol (b) — were recorded at the time as all zero except chorus at 1.5x
under protocol (b), which had the same four gaps, for totals of "34 expanded
routes, 3,820 loaded gaps, 8 over capacity". **Those totals cannot be audited
and should be treated as unverified.** The result JSON
they were summed from was not committed and no longer exists, and the route
count does not reconcile with this note's own tables: the Phase 1 tables hold
31 expanded routes that produced a board, or 35 counting the omitted `rigid`
rows for boards 00 and 01. Nothing below depends on the 3,820; the per-row
figures in the table are what the conclusions rest on.

Every 1x row is zero, as it must be for a board with no clearance errors
between its own traces — that is the metric's control. The four chorus gaps
are sub-0.5 mm slots between adjacent parts (`C11`–`J2` is 0.18 mm wide at 1x)
that carry nothing at 1x and one trace each at 1.5x: exactly the "topology
found with free space that cannot exist at final size" the issue was worried
about. It is real, it is 0.6% of that route's loaded gaps (4 of 688), and it
was recorded as recurring on chorus's protocol (b) 1.5x route, which is among
the rows that can no longer be checked.

Two readings, and the second is the more useful.

**Shrinking would be mostly geometry, not re-topology.** In particular the one
route worth shrinking — board 03 at rigid 1.5x — has no gap over capacity and
its tightest loaded gap (0.55 mm at 1x, needing 0.5 mm) fits. This is the
evidence behind #6310. It is thinner than it sounds: this metric sees
inter-footprint channels only, and says nothing about whether the repair at
every pad converges.

**Inter-footprint channels are nowhere near full, at any scale, on the boards
that are partial.** The tightest loaded gap on board 05 has over a millimetre
to spare; the tightest on chorus carry one to three traces. Channel width
between parts is the *only* capacity that expanding a placement increases, and
it is not what those two boards are short of.

Approximations, all of which limit what the metric can see: pad bounding boxes
stand in for obstacles; only footprint-to-footprint gaps are cut, not
footprint-to-board-edge; vias sitting in a gap are not charged; and capacity
inside a pin field (between the pads of one part) is not measured at all — it
does not scale, so it is outside what this experiment can change.

## Rigid mode on the partial boards (Issue #6312)

#6304 left one question open and ranked it first: does spreading a placement
with decoupling held rigid to its IC help a board the shipped router cannot
finish? This section measures it on board 05. chorus-test was not routed; why
is under ["chorus-test"](#chorus-test-a-clustering-rule-and-no-route).

**Result: no, under protocol (a) at a 1200 s budget.** On board 05, with
placement-delta feedback off, no `rigid` row finishes more routed nets than
1x, at any of the five scales run
(1.1, 1.2, 1.25, 1.4, 1.5). 1x leaves 4 unfinished in each of three runs.
`rigid` leaves 6, 4, 5 and 6, 6, and 7 and 6 (at 1.1x, 1.2x, 1.25x twice,
1.4x, and 1.5x twice). `kct check` errors are 4 at 1x and 5 to 7 in every
`rigid` row; `kicad-cli` DRC errors are 0 in every feedback-off row.
`uniform` is no better (7, 6 and 5 unfinished at 1.1x, 1.25x and 1.5x). One
run per expanded row, except `rigid` 1.25x and 1.5x, which have two each.

Four things to hold against that sentence:

- It is board 05 only, and it says nothing about any other board.
- It is one budget. No row varied the 1200 s ceiling or the per-net iteration
  cap, and the two `rigid` 1.5x runs (7 and 6 unfinished) are the only
  feedback-off rows the budget cut short. See "Deadline and load" under
  ["Verdict under the decision rule"](#verdict-under-the-decision-rule).
- Expansion does change *which* nets are stuck, consistently: `PHASE_A` is
  unfinished at 1x and finished in 9 of the 10 feedback-off expanded rows,
  while `GND_SENSE` and `VM`, both finished at 1x, are each unfinished in 9
  of them. The total does not improve: on routed nets every expanded row
  leaves unfinished at least as many nets that 1x finished as it finishes
  nets that 1x did not. See ["Net by net"](#net-by-net).
- The count the earlier tables in this note use ("signal nets unfinished", the
  classifier's) reads one lower than the truth on most expanded rows, because
  the classifier files `VM` as a pour net and board 05 has no `VM` zone. By
  that count `rigid` 1.2x is one better than 1x (3 against 4). Both counts are
  in the table.

### What was run

Board 05 under protocol (a) (`--layers 4 --no-auto-layers
--deterministic-budget --per-net-iterations 1000000`), `--grid-policy area`,
`--timeout 1200`, `PYTHONHASHSEED=0`, one route at a time. `main` at
`ee1d255f` plus this branch (harness and tests only; nothing under `src/`),
C++ backend build version 49, macOS, 8 logical CPUs, KiCad 10.0.6, on
2026-10-10. The host was shared: the 1-minute load average at the start of a
row ranged from 5.6 to 28.2 and is in the table for every row.

| Block | Rows | Feedback | Why |
|---|---|---|---|
| P | 1x; `uniform` and `rigid` at 1.25x and 1.5x; seed 42 | off | The planned comparison. |
| D | 1x again, seed 42 | off | Does a row reproduce? |
| seed 43 | 1x, and `rigid` 1.5x | off | Does the seed change the route? |
| M | `uniform` 1.1x; `rigid` 1.1x, 1.2x, 1.4x; seed 42 | off | Rows whose grid plan matches 1x around the driver IC (trap 7 below). Chosen from the grid plan before routing, not from results. |
| R | `rigid` 1.25x again, seed 42 | off | A second run of a `rigid` row at one of the two scales the decision rule names; in block P it came out level with 1x on the classifier's count. It is not the row closest to 1x: that is `rigid` 1.2x in block M, which the load column puts before this run and which has no second run. |
| S | 1x and `uniform` 1.5x, seed 42 | default | Continuity with #6304, which left feedback on. |

Feedback is off in the primary rows because placement-delta feedback moves
footprints, and an experiment about placement has to route the placement it
set.

**What was cut from the plan, and what was added.** The plan was block P at
three seeds (42, 43, 44), block D, block S, and a chorus block. Cut: seed 44
entirely, seed 43 except the two rows above, and every chorus route. Added:
blocks M and R. The seeds were cut because, in the one pair compared at the
time, a second seed reproduced the first seed's route exactly (next
subsection but one), which suggested that further seeds would be repeats.
The seed-43 run of that pair is not committed, and the committed seed-43 run
at 1x does not match seed 42 (120 vias and 1108.0 mm against 122 and 1123.4),
so the committed data does not show that on its own. M spends the time on
rows that answer a question the planned rows could not (trap 7), and R on a
second run of `rigid` 1.25x. R did not repeat the closest row. The row
closest to 1x is `rigid` 1.2x (4 routed nets unfinished against 4; 3 against
4 on the classifier's count), and by the load column block M, which holds
it, ran before block R (`rigid` 1.4x ends at 15.49; `05_off_s42_rep2` starts
at 15.05). So 1.2x was in hand when 1.25x was repeated, and it has one run.

```bash
uv run kct build-native --check
W=/tmp/ers-6312
X="--layers 4 --no-auto-layers --deterministic-budget --per-net-iterations 1000000"
run() {   # name seed scales modes extra [--append]
  uv run python scripts/research/expand_route_experiment.py route \
      --boards 05 --grid-policy area --timeout 1200 --seed "$2" \
      --scales "$3" --modes "$4" --extra "$5" $6 \
      --work-dir "$W/$1" --json "$W/05_$1.json"
}
OFF="$X --no-placement-delta-feedback"
run off_s42         42 1.0,1.25,1.5 uniform "$OFF"            # P
run off_s42         42 1.25,1.5     rigid   "$OFF" --append
run off_s42_rep     42 1.0          uniform "$OFF"            # D
run off_s43         43 1.0          uniform "$OFF"
run off_s43         43 1.5          rigid   "$OFF" --append
run on_s42          42 1.0,1.5      uniform "$X"              # S
run off_s42_matched 42 1.1          uniform "$OFF"            # M
run off_s42_matched 42 1.1,1.2,1.4  rigid   "$OFF" --append
run off_s42_rep2    42 1.25         rigid   "$OFF"            # R
```

One work directory and one JSON file per seed and per repeat: a run directory
is `<work-dir>/<board>-<mode>-<scale>`, with no seed in it, and it is deleted
at the start of each run. The harness now passes `--no-cache` itself. The
result files are committed unedited in
[`data/research/expand_route_shrink/`](../../data/research/expand_route_shrink/).
Every number in the results table and the net matrix below is in one of
them, and those two tables are the output of the command below. That is not
true of the seeds table: one of its rows is a run whose JSON was not
committed, and its last column was read from routed boards that are not
committed. Both are marked there.

```bash
D=data/research/expand_route_shrink
uv run python scripts/research/expand_route_experiment.py summary \
    $D/05_off_s42.json $D/05_off_s42_rep.json $D/05_off_s43.json \
    $D/05_off_s42_matched.json $D/05_off_s42_rep2.json $D/05_on_s42.json \
    --exclude-nets PHASE_A,PHASE_B,PHASE_C --zone-nets GND,+5V,V3P3,VIN --net-matrix
```

(`--zone-nets` supplies the board's zone nets for the rows written before the
harness recorded them itself. The routed boards are not committed.)

### Four more measurement traps (Issue #6312)

Numbered on from the four above. Each is handled in the harness and pinned by
a test; three are filed because they are not only harness hazards.

5. **A repeated row can be a cache replay.** `kct route` caches results keyed
   on the board's bytes and the command line. The first same-seed repeat here
   came back in 66 s with `Cache HIT ... Original compute time: 588369ms`: it
   had routed nothing. The harness now passes `--no-cache` and records
   `cache_hit` per row. This bears on #6304's remark, under "Run-to-run
   variance", that two re-runs of board 01 "reproduced the table exactly":
   whether those were cache hits was not checked and can no longer be.
6. **The harness's "timed out" flag was set on every row of the first runs
   here, none of which had timed out.** `--deterministic-budget` with
   `--timeout` prints a startup warning that quotes the marker the harness
   searched for (`'[deterministic-budget] stage deadline fired' log line will
   mark ...`). It now matches the real line only. In the table below "Timed out: yes" means a stage was cut short by
   the budget (here, always pour stitching, `unconnected pour link(s)
   (deadline)`) or the run hit the hard deadline.
7. **Rows at one coarse pitch are not grid-matched.** The auto grid also gives
   each dense part its own finer zone, and the pitch of that zone depends on
   where the part lands. On board 05 the driver IC `U2` (59 pads; `PHASE_A`,
   `PHASE_B`, `PHASE_C`, `EN_B`, `nFAULT` and `nTRIP` each have a pad on it)
   gets a 0.05 mm zone (48,555 cells) at 1x and a
   0.005 mm zone (4,845,540 cells) at some scales and not others, with the
   coarse pitch 0.065 mm throughout. From `kct route --dry-run` (the harness's
   new `plan` subcommand; all zones for all rows are in
   `05_grid_plan_scan.json`):

   | Scale | `uniform`: `U2` zone mm | cell estimate | `rigid`: `U2` zone mm | cell estimate |
   |---|---|---|---|---|
   | 1 | 0.05 | 3,932,679 | (same board) |  |
   | 1.05 | 0.005 | 8,856,279 | 0.005 | 8,905,820 |
   | 1.1 | 0.05 | 4,239,955 | 0.05 | 4,239,320 |
   | 1.15 | 0.05 | 4,388,452 | 0.005 | 9,213,265 |
   | 1.2 | 0.005 | 9,382,926 | 0.05 | 4,596,822 |
   | 1.25 | 0.005 | 9,601,586 | 0.005 | 9,596,006 |
   | 1.3 | 0.05 | 4,840,014 | 0.0125 | 5,567,343 |
   | 1.35 | 0.005 | 9,988,909 | 0.05 | 5,197,783 |
   | 1.4 | 0.005 | 10,160,179 | 0.05 | 5,368,495 |
   | 1.45 | 0.05 | 5,551,221 | 0.005 | 10,382,896 |
   | 1.5 | 0.05 | 5,792,108 | 0.005 | 10,577,933 |

   So of the planned rows only `uniform` 1.5x shares 1x's `U2` zone; `uniform`
   1.25x and both `rigid` rows were routed with `U2` on a zone of a hundred
   times the cells, and `rigid` 1.5x (both runs) ran into the budget during pour
   stitching. Block M adds `rigid` rows at 1.1x, 1.2x and 1.4x and a `uniform`
   row at 1.1x, where the `U2` zone is 0.05 mm as at 1x. Other zones still
   differ from row to row (`D3` is 0.04 mm at 1x and 0.025 mm in most expanded
   plans; the zone count runs from 24 to 41), so "matched" here means the
   `U2` zone and the coarse pitch, not the whole plan. #6304's statement that
   its protocol-(a) pairs were "grid-matched" rested on the coarse pitch
   alone. Filed as #6328.
8. **"Signal nets unfinished" leaves out a net that has no pour.** The stuck
   classifier labels any incomplete power-typed net `pour_discontinuous`,
   zone or no zone, and the harness subtracts that class. Board 05's zones
   are `GND`, `+5V`, `V3P3` and `VIN`; `VM` has none, so a stranded `VM` pad
   is a routing failure that the column did not count. `kct check` does count
   it (`connectivity: Net 'VM' is partially routed`), and `kct board-view
   list` reports its missing links pad to pad. Rows now record `zone_nets`
   and the summary prints a zone-checked count beside the classifier's. On
   every feedback-off row the zone-checked count equals `kct check`'s
   `connectivity` error count. Filed as #6327.

### Seeds are not samples here; repeats are, weakly

The plan was three seeds as three samples. The check that had to pass first
did not:

| Run | Seed | Load 1 min, start -> end | Vias | Wire mm | Unfinished nets | Copper compared with the first row |
|---|---|---|---|---|---|---|
| `05_off_s42`, 1x | 42 | 5.61 -> 13.01 | 122 | 1123.4 | `EN_B`, `PHASE_A`, `PHASE_B`, `PHASE_C` | -- |
| **not committed** (run before trap 6 was fixed; no committed file holds this row) | 43 | 18.37 -> 8.35 | 122 | 1123.4 | the same four | **identical on all 31 nets** |
| `05_off_s42_rep`, 1x (block D) | 42 | 25.1 -> 24.82 | 116 | 1123.5 | the same four | differs on 4 nets: `+5V`, `GND`, `GND_SENSE`, `VM` |
| `05_off_s43`, 1x | 43 | 24.82 -> 27.09 | 120 | 1108.0 | the same four | differs on the same 4 nets |

(Copper was compared net by net, segments and vias, on the routed boards,
which are not committed. The load, via, wirelength and unfinished-net cells
of the three committed rows are in the JSON. Every cell of the uncommitted
row, and the whole last column, can be checked against no committed file.)

- **A different seed gave the same route, to the last segment, in one pair of
  runs, and the seed-43 run of that pair is the uncommitted row.** `--seed`
  only calls `random.seed()`; nothing here shows it reaching this code path
  with the C++ backend. The committed seed-43 run does differ from the first
  row (120 vias and 1108.0 mm against 122 and 1123.4), as the same-seed
  repeat does (116 and 1123.5). So the committed rows show runs differing
  both at one seed and across two, and cannot separate the seed's effect
  from run-to-run variation. "The seed does not vary the route" rests on the
  one uncommitted pair.
- **The same seed gave a different route in block D**, on four of the 31 nets
  that carry copper. The two seed-42 logs first differ inside the negotiated
  loop (iteration 3 reports `overflow=73` in one and `overflow=110` in the
  other). The two runs that agree ran at lower load than the two that do not,
  which would fit a wall-clock-bounded stage, but that is four runs and load
  was not varied on purpose. Filed as #6329.
- **None of it changed which nets finish at 1x.** Three committed runs, the
  same four nets each time. At `rigid` 1.5x the two runs differ by one net
  (`RESET`), and so do the two runs at `rigid` 1.25x (also `RESET`; their copper
  differs on 5 nets: `+5V`, `GND`, `RESET`, `SPEED`, `VM`).

So the rows below are not replicated across seeds. The uncommitted pair
suggests that running seeds 43 and 44 through the whole matrix would have
produced repeats, not independent samples; the committed rows neither
confirm nor refute that. A repeat is still worth something, because repeats
do vary; the two expanded rows with a second run are `rigid` 1.25x and
`rigid` 1.5x. **Treat a difference of one net between two rows as within
the noise.**

### Results

`...without PHASE` drops `PHASE_A`, `PHASE_B` and `PHASE_C`: #6315 shows
`PHASE_C` cannot be routed whenever `PHASE_B` is, on an otherwise empty board
at 1x, so those nets can be stuck for a reason no placement change touches.
"Signal nets unfinished" is the classifier's count, as used in the rest of
this note; "routed nets unfinished" is the zone-checked one (trap 8).

| File | Scale | Mode | Feedback | Seed | Signal nets unfinished | ...without PHASE_A/PHASE_B/PHASE_C | Routed nets unfinished (zone-checked) | ...without PHASE_A/PHASE_B/PHASE_C | kct check err | kicad-cli DRC err | Vias | Vias / routed connection | Grid mm | Cell estimate | rc | Timed out | Cache hit | Wall s | Load 1 min, start -> end | Snapshot state |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 05_off_s42 | 1 | uniform | off | 42 | 4 | 1 | 4 | 1 | 4 | 0 | 122 | 0.91 | 0.065 | 3,932,679 | 2 | no | no | 553.1 | 5.61 -> 13.01 | -- |
| 05_off_s42 | 1.25 | uniform | off | 42 | 5 | 3 | 6 | 4 | 6 | 0 | 107 | 0.817 | 0.065 | 9,601,586 | 2 | no | no | 914.5 | 13.01 -> 6.04 | -- |
| 05_off_s42 | 1.5 | uniform | off | 42 | 4 | 2 | 5 | 3 | 5 | 0 | 102 | 0.773 | 0.065 | 5,792,108 | 2 | no | no | 956.8 | 6.04 -> 9.59 | -- |
| 05_off_s42 | 1.25 | rigid | off | 42 | 4 | 2 | 5 | 3 | 5 | 0 | 126 | 0.955 | 0.065 | 9,596,006 | 2 | no | no | 1027.8 | 9.59 -> 11.51 | -- |
| 05_off_s42 | 1.5 | rigid | off | 42 | 6 | 4 | 7 | 5 | 7 | 0 | 65 | 0.739 | 0.065 | 10,577,933 | 4 | yes | no | 1188.8 | 11.51 -> 28.62 | -- |
| 05_off_s42_rep | 1 | uniform | off | 42 | 4 | 1 | 4 | 1 | 4 | 0 | 116 | 0.866 | 0.065 | 3,932,679 | 2 | no | no | 785.7 | 25.1 -> 24.82 | -- |
| 05_off_s43 | 1 | uniform | off | 43 | 4 | 1 | 4 | 1 | 4 | 0 | 120 | 0.896 | 0.065 | 3,932,679 | 2 | no | no | 605.8 | 24.82 -> 27.09 | -- |
| 05_off_s43 | 1.5 | rigid | off | 43 | 5 | 3 | 6 | 4 | 7 | 0 | 78 | 0.886 | 0.065 | 10,577,933 | 4 | yes | no | 1162.3 | 27.09 -> 8.55 | -- |
| 05_off_s42_matched | 1.1 | uniform | off | 42 | 7 | 4 | 7 | 4 | 7 | 0 | 118 | 0.908 | 0.065 | 4,239,955 | 2 | no | no | 1157.4 | 28.19 -> 20.59 | -- |
| 05_off_s42_matched | 1.1 | rigid | off | 42 | 5 | 3 | 6 | 4 | 6 | 0 | 107 | 0.843 | 0.065 | 4,239,320 | 2 | no | no | 1146.4 | 20.59 -> 17.37 | -- |
| 05_off_s42_matched | 1.2 | rigid | off | 42 | 3 | 1 | 4 | 2 | 5 | 0 | 128 | 0.977 | 0.065 | 4,596,822 | 4 | no | no | 789.1 | 17.37 -> 7.72 | -- |
| 05_off_s42_matched | 1.4 | rigid | off | 42 | 5 | 3 | 6 | 4 | 7 | 0 | 130 | 0.985 | 0.065 | 5,368,495 | 2 | no | no | 728.4 | 7.72 -> 15.49 | -- |
| 05_off_s42_rep2 | 1.25 | rigid | off | 42 | 5 | 3 | 6 | 4 | 6 | 0 | 122 | 0.931 | 0.065 | 9,596,006 | 2 | no | no | 897.5 | 15.05 -> 9.06 | -- |
| 05_on_s42 | 1 | uniform | default | 42 | 4 | 1 | 4 | 1 | 5 | 0 | 82 | 0.901 | 0.065 | 3,932,679 | 4 | yes | no | 1188.1 | 8.55 -> 23.64 | -- |
| 05_on_s42 | 1.5 | uniform | default | 42 | 4 | 2 | 5 | 3 | 10 | 320 | 63 | 1.26 | 0.065 | 5,792,108 | 124 | yes (graded best-so-far) | no | 1205.9 | 23.64 -> 19.87 | -- |

Reading it, feedback-off rows only:

- **1x:** 4 unfinished in all three runs, the same four nets (`EN_B`,
  `PHASE_A`, `PHASE_B`, `PHASE_C`), 4 `kct check` errors.
- **`rigid`**, at 1.1x, 1.2x, 1.25x (two runs), 1.4x, 1.5x (two runs):
  routed nets unfinished 6, 4, 5 and 6, 6, 7 and 6. By the classifier's count
  5, 3, 4 and 5, 5, 6 and 5. `kct check` errors 6, 5, 5 and 6, 7, 7 and 7.
  One row is level with 1x on routed nets (1.2x); none is better. One row is
  better on the classifier's count (1.2x, 3 against 4), and that row has `VM`
  unfinished and one more `kct check` error than 1x.
- **`uniform`**, at 1.1x, 1.25x, 1.5x: routed nets unfinished 7, 6, 5; the
  classifier's count 7, 5, 4; `kct check` errors 7, 6, 5.
- **`rigid` against `uniform` at one scale** (routed nets unfinished): 6
  against 7 at 1.1x, 5 and 6 against 6 at 1.25x, 7 and 6 against 5 at 1.5x.
  No consistent order.
- **Without the `PHASE` nets** every expanded row is worse than 1x on routed
  nets: 1 at 1x; 4, 2, 3 and 4, 4, 5 and 4 for `rigid`; 4, 4, 3 for
  `uniform`. Taking the `PHASE` nets out makes expansion look worse, not
  better, because `PHASE_A` is the net expansion most reliably finishes.
- **The finer `U2` zone is not what makes the expanded rows worse.** Rows on
  1x's 0.05 mm zone: `uniform` 1.1x and 1.5x (7, 5), `rigid` 1.1x, 1.2x, 1.4x
  (6, 4, 6). Rows on the 0.005 mm zone: `uniform` 1.25x (6), `rigid` 1.25x
  (5, 6), `rigid` 1.5x (7, 6). Both groups sit at or above 1x's 4.
- **New error types:** `kct check` reports one `hole_to_hole_clearance` error
  in three `rigid` rows (1.2x, 1.4x, and the second run at 1.5x) and none in
  any 1x or `uniform` feedback-off row. Every other feedback-off `kct check`
  error is `connectivity`.
- **Time:** 553 to 786 s at 1x, 728 to 1189 s expanded. Both `rigid` 1.5x
  runs were cut short in pour stitching by the budget; no feedback-off row
  hit the hard deadline (`rc` 124).

**Vias per routed connection** does not separate the arms. It is 0.87 to 0.91
at 1x (three runs) and 0.74 to 0.99 across the feedback-off expanded rows,
on both sides of the 1x range. The denominator is all connections including
pour links, and the two `rigid` 1.5x rows lost about a third of their
connections to the cut-short pour stitching (88 of 142 routed, against 127 to
134 elsewhere), so the ratio is not a clean per-route figure on those rows.

### Net by net

Each net that is unfinished in any row, and its state in every row. A cell is
`ok` or the classifier's class, then that net's via count on that board.
`GND` and `+5V` have zones; `VM` does not.

| Net | 05_off_s42 uniform 1 | 05_off_s42 uniform 1.25 | 05_off_s42 uniform 1.5 | 05_off_s42 rigid 1.25 | 05_off_s42 rigid 1.5 | 05_off_s42_rep uniform 1 | 05_off_s43 uniform 1 | 05_off_s43 rigid 1.5 | 05_off_s42_matched uniform 1.1 | 05_off_s42_matched rigid 1.1 | 05_off_s42_matched rigid 1.2 | 05_off_s42_matched rigid 1.4 | 05_off_s42_rep2 rigid 1.25 | 05_on_s42 uniform 1 | 05_on_s42 uniform 1.5 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| +5V | ok (16 v) | ok (16 v) | ok (17 v) | ok (16 v) | pour_discontinuous (0 v) | ok (16 v) | ok (16 v) | pour_discontinuous (0 v) | ok (17 v) | ok (16 v) | pour_discontinuous (15 v) | ok (17 v) | ok (16 v) | pour_discontinuous (0 v) | pour_discontinuous (0 v) |
| EN_B | congestion_saturated (0 v) | placement_bound (0 v) | congestion_saturated (0 v) | ok (4 v) | placement_bound (0 v) | congestion_saturated (0 v) | congestion_saturated (0 v) | placement_bound (0 v) | ok (4 v) | placement_bound (0 v) | congestion_saturated (0 v) | placement_bound (0 v) | ok (4 v) | placement_bound (0 v) | placement_bound (0 v) |
| EN_C | ok (2 v) | ok (4 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) | placement_bound (0 v) | ok (2 v) | ok (6 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) |
| GND | pour_discontinuous (25 v) | pour_discontinuous (23 v) | pour_discontinuous (23 v) | pour_discontinuous (24 v) | pour_discontinuous (0 v) | pour_discontinuous (25 v) | pour_discontinuous (25 v) | pour_discontinuous (0 v) | pour_discontinuous (24 v) | pour_discontinuous (24 v) | pour_discontinuous (24 v) | pour_discontinuous (24 v) | pour_discontinuous (24 v) | pour_discontinuous (0 v) | pour_discontinuous (0 v) |
| GND_SENSE | ok (6 v) | congestion_saturated (2 v) | congestion_saturated (2 v) | congestion_saturated (4 v) | congestion_saturated (4 v) | ok (6 v) | ok (6 v) | congestion_saturated (4 v) | placement_bound (6 v) | congestion_saturated (2 v) | ok (6 v) | congestion_saturated (4 v) | congestion_saturated (4 v) | ok (6 v) | placement_bound (2 v) |
| IREF | ok (2 v) | ok (3 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (2 v) | ok (6 v) | ok (4 v) | ok (2 v) | ok (4 v) | congestion_saturated (0 v) | ok (2 v) | ok (2 v) | ok (2 v) |
| PHASE_A | budget_starved (0 v) | ok (4 v) | ok (1 v) | ok (1 v) | ok (1 v) | budget_starved (0 v) | budget_starved (0 v) | ok (1 v) | budget_starved (0 v) | ok (2 v) | ok (4 v) | ok (2 v) | ok (1 v) | placement_bound (0 v) | ok (1 v) |
| PHASE_B | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) |
| PHASE_C | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | budget_starved (0 v) | placement_bound (0 v) | budget_starved (0 v) |
| RESET | ok (2 v) | placement_bound (4 v) | ok (6 v) | ok (14 v) | placement_bound (2 v) | ok (2 v) | ok (2 v) | ok (4 v) | budget_starved (4 v) | placement_bound (2 v) | ok (6 v) | ok (9 v) | placement_bound (10 v) | ok (2 v) | ok (6 v) |
| V3P3 | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | pour_discontinuous (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | pour_discontinuous (0 v) |
| VIN | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | ok (0 v) | pour_discontinuous (0 v) |
| VM | ok (16 v) | pour_discontinuous (4 v) | pour_discontinuous (4 v) | pour_discontinuous (12 v) | pour_discontinuous (7 v) | ok (10 v) | ok (14 v) | pour_discontinuous (6 v) | ok (14 v) | pour_discontinuous (8 v) | pour_discontinuous (16 v) | pour_discontinuous (18 v) | pour_discontinuous (12 v) | ok (16 v) | pour_discontinuous (4 v) |
| nFAULT | ok (3 v) | ok (6 v) | ok (6 v) | congestion_saturated (0 v) | ok (4 v) | ok (3 v) | ok (3 v) | ok (6 v) | placement_bound (0 v) | ok (6 v) | ok (6 v) | ok (6 v) | congestion_saturated (0 v) | ok (4 v) | ok (6 v) |
| nTRIP | ok (8 v) | ok (4 v) | ok (8 v) | ok (6 v) | placement_bound (2 v) | ok (8 v) | ok (8 v) | placement_bound (4 v) | ok (3 v) | ok (5 v) | ok (6 v) | ok (6 v) | ok (6 v) | ok (8 v) | ok (8 v) |

What repeats across the 10 feedback-off expanded rows:

- `PHASE_B` and `PHASE_C` are unfinished in all 13 feedback-off rows (#6315).
- `PHASE_A`: unfinished in 3 of 3 runs at 1x; **finished in 9 of 10** expanded
  rows (not at `uniform` 1.1x). Its route has 1 via in five of those rows, 2
  in two and 4 in two.
- `GND_SENSE`: finished in 3 of 3 at 1x; **unfinished in 9 of 10** expanded
  rows (finished at `rigid` 1.2x).
- `VM`: finished in 3 of 3 at 1x; **unfinished in 9 of 10** expanded rows
  (finished at `uniform` 1.1x).
- `EN_B`: unfinished in 3 of 3 at 1x; finished in 3 of 10 expanded rows
  (both runs of `rigid` 1.25x, and `uniform` 1.1x).
- Finished in every 1x run and unfinished in some expanded rows: `RESET` (5
  of 10), `nFAULT` (3), `nTRIP` (2), `EN_C` (1), `IREF` (1).

Counted on routed nets (the zone-checked count), and against the four nets
1x leaves unfinished: an expanded row finishes one or two of those four
(`PHASE_A` alone in seven rows, `EN_B` alone at `uniform` 1.1x, both in the
two `rigid` 1.25x runs) and leaves unfinished one to four nets that 1x
finished. The one is `VM` at `rigid` 1.2x; the fours are `rigid` 1.5x at
seed 42, `uniform` 1.1x and the second `rigid` 1.25x run. On the
classifier's count, which does not see `VM`, the losses are zero to four:
one fewer in the nine rows that leave `VM` unfinished, and still four at
`uniform` 1.1x, which finishes `VM`. Neither figure counts the zone nets
`+5V` and `V3P3`, which this section leaves out everywhere.

Renders (`kct board-view net`) were made for every net whose state differs
between 1x and an expanded row of block P; five are committed under
`data/research/expand_route_shrink/renders/`. **What follows is read from
images and router logs. Each is a hypothesis, not a measurement.**

- **`PHASE_A` and `GND_SENSE` look like one trade, at one spot.** At 1x
  (`s42_PHASE_A_uniform-1.png`, `s42_GND_SENSE_uniform-1.png`) `PHASE_A` has
  no copper at all, and a `GND_SENSE` via sits just left of `U2` pins 5 to 7,
  between `U2` and `C2`. The 1x log says `PHASE_A`'s search gave up with "all
  via candidates blocked by stored-via geometry (blocking net GND_SENSE)". In
  the expanded rows (`s42_PHASE_A_rigid-1.25.png`,
  `s42_GND_SENSE_rigid-1.25.png`) a `PHASE_A` via sits at that spot and the
  net runs to `J2` on the back layer, while `GND_SENSE` is left with one
  missing link of about 2 mm between two pins of `U2` itself (pin 7 to pin
  10 in the `rigid` 1.25x render, with the unrouted `PHASE_B` and `PHASE_C`
  pads between them). Two rows do not fit a strict one-or-the-other reading:
  `rigid` 1.2x finishes both nets (`PHASE_A` with 4 vias, `GND_SENSE` with
  6), and `uniform` 1.1x finishes neither.
- **Does `PHASE_A`'s route use a via in space that did not exist at 1x?
  It does not look like it.** In three of the four block-P expanded rows the
  route has a single via, 1.6 to 1.8 mm from the centre of pad `U2.5`. In the
  two `rigid` rows that via is in the gap between `U2` and `C2`, and `C2`
  rides in `U2`'s rigid cluster, so that gap is the 1x gap exactly. Replaying
  each via's offset from its nearest footprint onto the 1x board puts it at
  least 1.7 mm, centre to centre, from the nearest pad of any other footprint
  — room for a 0.8 mm via — and 0.17 to 0.36 mm from the edge of the
  `GND_SENSE` via that occupies the spot at 1x. So the space is there at 1x
  and another net is in it. (The fourth row, `uniform` 1.25x, routes
  `PHASE_A` with four vias, one of them midway between `C2` and `U2` where
  the replay depends on which part it is anchored to; that one is not
  decided by this check.) The check ignores traces and uses pad centres, so
  it can show that a spot is free of footprints at 1x, not that a route
  through it exists.
- **`EN_B`**, finished only at `rigid` 1.25x in block P, takes a via 1.6 mm
  from its `U2` pad and a long back-layer detour with four vias. Replayed at
  1x, all four via positions are more than 5 mm from any other footprint's
  pad. Again space that exists at 1x.
- **The nets that become stuck when expanded look stuck at a pad, not for
  want of room.** `RESET` at `uniform` 1.25x (`s42_RESET_uniform-1.25.png`) has
  copper at both ends of its one missing link, across open board: at `R5.2`
  a track reaches the pad, and at `U1.29` the copper stops at a via just
  short of the pad; that row's log reports "post-route
  clearance validation rejected every candidate" (via-to-pad gaps of 0.09 to
  0.11 mm) and then a connectivity regression in the optimize phase from 4 of
  4 pads to 2 of 4. `nFAULT` and `nTRIP` end on adjacent pins of `U2` (18 and
  19); the logs say "committing it would strand U2.18 (nFAULT)" and "U2.18
  has no legal exit, closed by DRIVER_SLEEP, U2, U2.17, U2.19, U2.29, nTRIP".
  These are escapes inside one part's pin field, which does not scale.

Taken together the renders point the same way as #6304's second reason: on
board 05 the contention is at `U2`'s pins, and spreading the placement moves
which net loses there. That is a reading of one board's images.

### Feedback at its default (block S)

Two rows, one run each, `uniform` only; a secondary check, not part of the
verdict.

- **1x:** the same four nets unfinished as with feedback off. 5 `kct check`
  errors (4 `connectivity`, 1 `hole_to_hole_clearance`), 0 from `kicad-cli`.
  The run used 1188 s of its 1200 s and pour stitching was cut short by the
  budget; it did not hit the hard deadline, so there is no timeout sidecar
  and no `snapshot_state` to report. The run directory's
  `_placement_delta.json` listed proposed moves and an empty `applied` list
  (read by hand; not in the committed JSON).
- **`uniform` 1.5x: a deadline artifact. Do not read its counts.** The run
  hit the hard deadline (`rc` 124) inside placement-delta feedback and the
  row is graded from the `_partial` board: 4 signal nets unfinished by the
  classifier's count, 5 routed nets, 10 `kct check` errors, 320 `kicad-cli`
  errors (199 `track_width`, 97 `clearance`, 24 `drill_out_of_range`) on raw
  copper that has had no optimisation. The row's `timeout_stage` is
  `placement-delta-feedback` and it carries **no `snapshot_state`**: the
  timeout sidecar had `snapshot_saved: false` and none of the #6314 fields
  (read from the sidecar by hand; the committed row predates the harness
  copying that field and holds only `timeout_sidecar: true` and
  `timeout_stage`),
  although a `_partial` board had been written at the deadline. So what that
  board holds (accepted copper or a trial's) is not known. Its footprints are
  at the input's positions (checked by hand). Filed as #6330; the harness now
  copies `snapshot_saved` into the row as well.

For what continuity is worth: the unfinished nets at 1x are the same with
feedback on and off, and the 1.5x artifact lists the same five nets as the
feedback-off `uniform` 1.5x row. Neither row is evidence that feedback
changes the comparison, and neither shows that it does not.

### chorus-test: a clustering rule, and no route

The default rule (anchor has 6 or more pads; a part with at most 2 pads joins
the nearest anchor within 3 mm that shares a net with it) clusters 2 of
chorus's 90 footprints. The harness now takes the three parameters on the
command line (`--anchor-min-pads`, `--satellite-max-pads`, `--max-gap-mm`),
records them in each `rigid` row, and has a `clusters` subcommand that prints
these counts without routing:

| `anchor_min_pads`, `satellite_max_pads`, `max_gap_mm` | chorus (90 footprints) | 03 (38) | 05 (42) |
|---|---|---|---|
| 6, 2, 3 (default; every `rigid` row in this note) | 1 cluster, 2 footprints | 4, 16 | 4, 14 |
| 3, 2, 3 | 8 clusters, 20 footprints | 5, 19 | 4, 14 |
| 3, 2, 5 | 11 clusters, 36 footprints | 8, 31 | 5, 22 |

(`clusters_a6_s2_g3.json`, `clusters_a3_s2_g3.json`, `clusters_a3_s2_g5.json`,
which also list every cluster's members and the nets each shares with its
anchor.)

What excludes chorus under the default rule is the anchor threshold: its ICs
have five pads (8 parts with 5 pads, 2 with 4, 1 with 3, against 3 parts with
20 or more). Lowering it to 3 leaves board 05's clusters unchanged at 3 mm.

**The rule to use on chorus, if it is routed, is 3, 2, 3 — and it is weaker
than the count suggests.** Of the 12 parts it attaches to an anchor, every
one shares only supply or ground nets with it: 4 share both a supply and a
ground (`C38`-`U3`, `C29`-`U7`, `C31`-`Y2`, `C13`-`U1`: plausibly
decoupling), 3 share a supply only, and 5 share a ground only. So on chorus
the shared-net condition, which was meant to separate "this cap decouples
that IC" from "this part happens to sit beside it", mostly reduces to
"nearest part with three or more pads". Widening the gap to 5 mm makes it
worse: 25 attached parts, 24 on rails only, 13 on a ground only. For
comparison, board 05's default clusters attach 10 parts, 8 on rails only, but
each of those 8 shares both a supply and `GND` with its IC.

**chorus was not routed.** Block C (1x, and 1.5x in both modes) was cut for
time: board 05 came first, a chorus row is expected to run to its 1200 s
budget (every chorus row in #6304 ran to its budget), and the rule's clusters cover 20 of 90 footprints
with only 4 attachments that look like decoupling. A chorus `rigid` run would
also need trap 7 checked first: nobody has looked at its grid plan across
scales.

### Verdict under the decision rule

The rule set in #6312, as written there:

> - *Supports revisiting:* `rigid` at 1.25x or 1.5x finishes more signal nets
>   than 1x at the same seed, in at least two seeds, on rows that did not hit
>   the deadline or ran at comparable load, with no increase in errors on
>   either DRC engine.
> - *Closes it:* `rigid` is level with or worse than 1x. [...]
> - *Inconclusive, say so:* the difference is within what block D or the
>   seed-to-seed spread shows, or the grid pitch differs between the rows
>   compared.

Seeds were not shown to be independent samples (above), so "in at least two
seeds" is read here as "in at least two runs". That substitution is this
note's, not the rule's.

**It closes the question for board 05, under protocol (a) at a 1200 s
budget: on routed nets `rigid` is level with or worse than 1x in every run,
and better in none.** That is one board, one effective seed, mostly single
runs, and a budget that was not varied. Read on the feedback-off rows:

| | 1x (3 runs) | `rigid` 1.25x (2 runs) | `rigid` 1.5x (2 runs) |
|---|---|---|---|
| Signal nets unfinished (classifier) | 4, 4, 4 | 4, 5 | 6, 5 |
| ...without `PHASE_A/B/C` | 1, 1, 1 | 2, 3 | 4, 3 |
| Routed nets unfinished (zone-checked) | 4, 4, 4 | 5, 6 | 7, 6 |
| ...without `PHASE_A/B/C` | 1, 1, 1 | 3, 4 | 5, 4 |
| `kct check` errors | 4, 4, 4 | 5, 6 | 7, 7 |
| `kicad-cli` DRC errors | 0, 0, 0 | 0, 0 | 0, 0 |

No `rigid` run at 1.25x or 1.5x finishes more nets than 1x on either count,
with or without the `PHASE` nets, and `kct check` errors are higher in all
four. The best single cell is the first `rigid` 1.25x run on the classifier's
count: level with 1x, and one worse in its repeat.

Three qualifications, all stated because the rule asks for them:

- **Grid.** The 1.25x and 1.5x `rigid` rows were routed with `U2` on a zone
  of a hundred times the cells it has at 1x (trap 7), which by the letter of the rule
  makes those two comparisons inconclusive. The rows chosen to remove that
  difference do not change the answer: `rigid` at 1.1x, 1.2x and 1.4x leaves
  6, 4 and 6 routed nets unfinished against 4. The one cell in the whole
  table where `rigid` beats 1x is 1.2x on the classifier's count (3 against
  4). That is one net in one run, the size of the difference between repeats
  of the same row at 1.25x and at 1.5x; the net it gains is `PHASE_A` and the
  net it loses, `VM`, is one the classifier's count cannot see; and the row
  has 5 `kct check` errors against 4. It does not meet "more nets and no more
  errors".
- **Replication.** One route per row, with second runs of three rows, and a
  spread of one net between repeats. Against that spread: on routed nets
  `rigid` is two to three worse than 1x at 1.5x and one to two worse at
  1.25x; on the classifier's count it is one to two worse at 1.5x and level
  or one worse at 1.25x. Only the first of those four is clearly outside the
  spread, and both of its runs are the rows the budget cut short (next
  point). Two worse than 1x also appears in the single runs at 1.1x and 1.4x
  and in one of the two runs at 1.25x, none of which was cut short. "Level
  with or worse" is what the rule asks about, and it holds in every run.
  "Worse" by more than the one-net spread, in both runs of a repeated row,
  is seen only at 1.5x, on the rows that ran out of budget; at 1.25x the two
  runs are one and two worse. So "worse" is not established beyond the
  spread on any repeated row that ended inside its budget.
- **Deadline and load.** The rule's first branch asks for rows that did not
  hit the deadline or ran at comparable load, and the rows this verdict
  leans on have to meet the same test. The two `rigid` 1.5x runs fail its
  first half, and load was not controlled in any row.
  They are the only feedback-off rows with `hit_timeout: true` (a stage cut
  short by the budget, here pour stitching; not the hard deadline): `rc` 4,
  88 of 142 connections routed, 1188.8 and 1162.3 s of 1200, with the
  1-minute load going 11.5 -> 28.6 in one and 27.1 -> 8.6 in the other. The
  grid point
  above already calls them inconclusive. As evidence they are the weakest
  rows in the table, and the 1.5x column of it is those two rows alone.
  The other eight feedback-off expanded rows have `hit_timeout: false`. Six
  of them ended with more than 170 s in hand (728 to 1028 s) and two took
  1146 and 1157 s. All eight leave at least as many routed nets unfinished
  as 1x's 4: `rigid` 6, 4, 5 and 6, 6 (at 1.1x, 1.2x, 1.25x twice, 1.4x);
  `uniform` 7, 6, 5. Load does not sort the rows: 1x left the same four nets
  from a starting load of 5.6, 25.1 and 24.8, and the three expanded rows
  that started at the lowest load (`uniform` 1.5x at 6.0, `rigid` 1.4x at
  7.7, the first `rigid` 1.25x run at 9.6) left 5, 6 and 5. So on the rows
  that ended inside the budget, neither the hard deadline nor host load
  accounts for the result.
  What is not excluded is the budget itself, because no row varied it. Every
  row ran with `--timeout 1200` and `--per-net-iterations 1000000` while the
  cell estimate rose to as much as 2.7 times 1x's, and none passed
  `--deterministic-rescue`, so any relief-rescue sub-search kept its 10 s
  wall-clock limit on a host whose 1-minute load ran from 5.6 to 28.6.
  Whether `rigid` 1.5x is worse than 1x or only did not finish in 1200 s is
  not decided by these rows.

With #6304, whose partial-board rows can no longer be relied on, what stands
is this section alone: on board 05, under protocol (a) at 1200 s, neither
`uniform` nor `rigid` spreading let the shipped router finish more routed
nets than it does at 1x.

### Not measured in #6312

- **chorus-test in `rigid` mode**, or at all. See above.
- **Any board but 05.** Nothing here says what `rigid` spreading does on a
  board whose stuck nets are not at one part's pins.
- **Independent replication.** The seed was not shown to be a source of
  independent variation: one pair, not committed, matched exactly, and the
  committed runs differ both at one seed and across two. So the rows are
  read as one route per row plus second runs of 1x (two), `rigid` 1.25x and
  `rigid` 1.5x. Those repeats differ from their twins, by at most one net in
  which nets finish.
- **A second run of `rigid` 1.2x.** It is the only row level with 1x on
  routed nets and the only one better than 1x on the classifier's count, and
  it has a single run. Block R repeated 1.25x.
- **A larger time or iteration budget, and `--deterministic-rescue`.** Every
  row ran with `--timeout 1200` and `--per-net-iterations 1000000`, without
  `--deterministic-rescue`. The two `rigid` 1.5x runs were cut short by that
  budget, and nothing here says what they, or any other expanded row, would
  do with more.
- **A fully grid-matched pair.** Block M matches the `U2` zone and the coarse
  pitch, not every zone. An explicit `--grid` would match everything and was
  not tried: #6304's protocol (c) could not finish board 05 at 1x on one.
- **Staging.** #6312's update notes one run that routed `PHASE_A` and
  `PHASE_B` first and left 3 signal nets unfinished against 6 in a single
  pass. Not repeated here, at any scale.
- **Whether a route found expanded survives shrinking.** No expanded row is
  better than 1x, so there was nothing to shrink.
- **Scales above 1.5x.** The router refuses board 05 at 2x under this grid
  policy (#6306).
- **Why `VM` and `GND_SENSE` copper differs between same-seed runs** (#6329),
  and whether those two nets' flips in the expanded rows are related to it.
  They are two of the four nets that vary between 1x repeats.

## Why it mostly does not work

Three reasons, in decreasing order of how much of the result they explain.

1. **Uniform scaling preserves exactly the thing that makes a net unroutable.**
   An affine map leaves the placement's topology untouched: which pins face
   which, the cyclic order of nets around every part, which nets must cross.
   A net that is stuck because its partner pad is on the far side of a bundle
   it has to cross is stuck at every scale; it needs a via, a layer, or a part
   rotated — not millimetres. Scaling only relieves *channel capacity*, and
   Phase 2 says channel capacity is not binding on boards 05 and chorus.
   (This is an explanation, not a law the data obeys everywhere: board 03's
   `USB_D+` is stuck at `uniform` 1x and 1.5x and routes at `uniform` 2x on
   the explicit grid.)
2. **Pin-field geometry does not scale, and that is where the density is.**
   The proposal anticipated this for `ESCAPE_BLOCKED` nets specifically. It is
   more general: a 0.5 mm-pitch QFP presents the same escape problem on a
   60 mm board and a 120 mm one.
3. **The router is budget-bound on precisely the boards that are partial, and
   expansion spends the budget.** (This is #6304's reading, from rows that
   are unverified since #6314. #6312's rows contradict it for board 05; see
   the end of this item.) The *coarse* grid grows as s² at fixed
   pitch, but the router's cell estimate does not, because on these boards
   much of it is fine-grid zones around individual parts, and a footprint does
   not grow when the placement is scaled. Board 05's logged estimate goes
   3.93 M → 5.79 M at 1.5x, which is 1.47x, not 2.25x: re-reading the grid
   plan from the router's log at this branch's head, the coarse 0.065 mm grid
   is 1.49 M → 3.36 M cells (the 2.25x) on top of about 2.44 M fine-zone cells
   at both scales (1.98 M of them one zone, `U3` at 0.005 mm).
   Chorus's estimate grows 1.61x (3.09 M → 4.98 M). No 2x estimate exists for
   either board. Paths are s times longer, and A\* expansions grow with both
   cells and path length. Under protocol (a), in #6304's rows, 11 of the 33
   unfinished signal nets at 1x across the two partial boards were
   `BUDGET_STARVED` before anything was expanded, and none of them completed.
   **#6312's committed rows do not bear this out on board 05.** With feedback
   off, `PHASE_A` is `budget_starved` at 1x in all three runs and finished in
   9 of the 10 expanded rows; the 1x runs used 553, 786 and 606 s of their
   1200 s, not all of it. (With feedback at its default, as in #6304, the
   one #6312 run of 1x did run to 1188 s, so #6304's rows reaching their
   budget is reproduced; that the routing itself needs the whole budget is
   not.) (The other two `budget_starved` nets, `PHASE_B`
   and `PHASE_C`, are unfinished in every row, at 1x and expanded; see
   #6315.) So on board 05 this reason is not supported as stated. Chorus was
   not re-run, and what a larger budget would do to the expanded rows was
   not measured.

Board 03 is the exception that fits the rule: one stuck net, a small board
that finishes well inside its budget at 1.5x, and — the part `rigid` supplies —
a placement change that is *not* purely affine, because the clusters keep
their internal geometry while the space between them opens. Reason 1 applies
to `uniform`; it applies less to `rigid`. That is the most interesting thing
#6304 did not get to test on the hard boards — and in #6304 board 03 was the
only board run in `rigid` mode on which the clustering rule groups more than
three footprints, so there "what `rigid` does" meant "what it did on board
03". (#6312 has since run board 05, which clusters 14 of its 42 footprints,
in `rigid` mode: level with or worse than 1x at a 1200 s budget. Chorus is
still untested.)

The anytime property — a complete, clean board at every step — was the
proposal's main attraction, and it depends on step 3 delivering a complete,
clean board to start from. On the boards that need help, it does not.

## Go / no-go

**No-go on Phases 3–4 as specified.** No shrink-loop prototype is built, and no
flag is added to `kct route`. One narrow probe is filed (#6310) instead of a
prototype.

| Question from the issue | Answer |
|---|---|
| Do boards that are partial at 1x reach 100% expanded? | No. Board 05, re-measured in #6312 with feedback off, under protocol (a) at 1200 s: 4 routed nets unfinished at 1x in three runs; 5 to 7 in three `uniform` rows and 4 to 7 in seven `rigid` runs, 1.1x to 1.5x. #6304's own rows (0 of 2 at 1.5x `uniform` under either protocol; board 05 9 and 9, chorus 30 → 34 under protocol b) are unverified since #6314. Not tested: 2x, a larger budget, and chorus in `rigid` mode. |
| Does anything complete expanded that does not at 1x? | Yes, on board 03. For signal nets, with an explicit grid only: rigid 1.5x is fully complete (27/27, 0 DRC both engines), and uniform 2x routes the same net with 0 DRC errors (26/27, `GND` pour discontinuous); the default grid already routes that net at 1x. For the `GND` pour, under the `area` policy too: both `rigid` rows are 27/27 against 26/27 at 1x. |
| Do boards that are complete at 1x stay complete? | Not reliably: board 04 loses a net at uniform 2x under both grid policies. Boards 02 and 03 stay signal-complete at every scale under the `area` policy. (Board 03 on the explicit grid is not complete at 1x; its rigid 2x row routes the 1x-stuck net and leaves two others unfinished.) |
| Do `ESCAPE_BLOCKED` nets fail to benefit, as predicted? | Untestable — none exist at 1x on this fleet. |
| Do `PLACEMENT_BOUND` nets benefit, as predicted? | On board 03, yes (1 of 1). On 05 and chorus, 4 of 15 under (a) and 5 of 26 under (b), with more nets newly stuck than freed across all classes (10 against 6, and 9 against 5). |
| Is shrinking geometry or re-topology? | Geometry, as far as this metric sees: 4 of 1,550 loaded gaps over 1x capacity in the tabulated expanded routes, all on chorus. |
| Minimum feasible scale per board, both DRC engines? | Not measured — it needs the shrink step. Only board 03 has an expanded starting point that is better than its 1x run: on the explicit grid, rigid 1.5x and (for signal nets) uniform 2x; under the `area` policy, the two `rigid` rows, by the `GND` pour alone (27/27 against 26/27). |

What would change the verdict, in order of cost:

1. **#6310 says go** — board 03's rigid 1.5x route survives a single shrink to
   1x with `USB_D+` intact and both DRC engines clean, with repair confined to
   trace ends at pads. *Answered: no-go under both the grid engine and the
   lattice protocol (see the #6310 section below).*
2. **`rigid` mode helps a board that is partial under the shipped router.**
   Measured on board 05 in #6312, and it does not: see
   ["Verdict under the decision rule"](#verdict-under-the-decision-rule).
   That removes board 05 from this list at the budget it was run with; a
   larger budget, and a second run of the one row level with 1x (`rigid`
   1.2x), are listed there as not measured. It leaves chorus, which was not
   routed; a rule that clusters 20 of its 90 footprints now exists
   (`--anchor-min-pads 3`), with the weakness described in
   ["chorus-test"](#chorus-test-a-clustering-rule-and-no-route).
3. **A board on which inter-footprint channel capacity is the binding
   constraint** — the `gaps` subcommand reporting at- or over-capacity gaps on
   a 1x partial route. None of the boards in the Phase 2 table is one. A dense
   two-layer board with long parallel buses between rows of parts is where to
   look.

## Refinements the data suggests

Ordered by how directly the measurements support them.

1. **If anything is scaled, `rigid` is the variant to try first — on thin
   evidence.** Most of the positives are `rigid` rows: board 03 fully complete
   at 1.5x on the explicit grid, its pour discontinuity cleared (27/27) in
   three rows, board 04 not regressing at 2x where `uniform` does. But not
   all: on board 03's explicit grid at 2x, `uniform` routes the stuck net with
   zero errors and `rigid` is the worst row in the table (2 unfinished, 2,750
   errors), so `uniform` wins that comparison outright. And the `rigid` wins
   come from one board: 03 is the only one run in `rigid` mode where the rule
   clusters more than three footprints, and board 04's win turns on a single
   part.
   `uniform` is electrically wrong in what it does to decoupling, which is a
   reason to prefer `rigid` independent of these counts. The mechanism is
   plausible too: a rigid-cluster spread is not affine, so it can change which
   channels exist, where a uniform scale only widens the ones already there.
   On the one partial board since measured it does not carry: board 05 in
   #6312 has `rigid` at 6, 5 and 6, and 7 and 6 routed nets unfinished at
   1.1x, 1.25x and 1.5x against `uniform`'s 7, 6 and 5, neither better than
   the other and neither better than 1x.
2. **The data does not locate a useful scale; cost argues for starting
   small.** The one row that suggests "1.5x good, 2x bad" is board 03 `rigid`
   on the explicit grid, a single run that used 862 s of 900 s. `uniform` on
   the same grid gains only at 2x, board 03 `rigid` at 2x under the `area`
   policy is 27/27 with zero errors, and board 04 `uniform` regresses at 2x.
   The two partial boards have no 2x data at all, so nothing here says the
   proposal's "~2x" is past a useful range for them. What is established is
   cost: at a fixed pitch the coarse grid is four times the cells at 2x, and
   the auto-grid refuses boards 04 and 05 at 2x outright. If this is
   revisited, sweep 1.1–1.5x first because it is cheaper and routable, not
   because 2x has been shown not to work.
3. **Expand virtually, in the global router, instead of geometrically.** The
   useful output of an expanded route is the list of places where the 1x board
   runs out of room. That list can be had without scaling anything: run the
   tile-based global router with inflated edge capacities and report the edges
   whose real capacity is exceeded. It costs no extra grid cells, needs no
   shrink loop, and is directly what `router/placement_nudge.py` and
   `router/auto_pcb_size.py` want as input. The routing-plan stage already
   reports overflow edges; whether they line up with the stuck nets is the
   first thing to check. Filed as #6309 — as an evaluation, because on today's
   fleet Phase 2 says that list may be nearly empty.
4. **Spread locally, where capacity binds, not globally.** If a gap is over
   capacity, move the two parts that bound it; do not scale the other forty.
   Global expansion pays for a coarse grid s² times larger to relieve gaps
   that were never tight.
5. **Treat "more room made it worse" as a router bug, not a curiosity.** A
   router whose completion is not monotone in available space will mislead any
   search over board size, including `auto_pcb_size.py`'s grow-on-failure loop,
   which assumes the opposite. Filed as #6307.
6. **Coarse-grid-then-refine is a speed lever worth a separate look.** Boards
   02 and 03 route 2–3x faster expanded on a coarse grid at equal quality. The
   same speed-up should be available at 1x by routing coarse first and
   refining, without moving any part. Not filed: it is an observation from two
   easy boards, not a measured proposal.

## Not measured

- **Board 05 and chorus at 2x**, and chorus in `rigid` mode. Board 05 in
  `rigid` mode was measured in #6312
  (["Rigid mode on the partial boards"](#rigid-mode-on-the-partial-boards-issue-6312));
  that section has its own list of what it did not cover. Chorus clusters 2
  of 90 footprints under the default rule and 20 of 90 with the anchor
  threshold lowered to 3 pads; it has not been routed under either.
- **Whether #6304's partial-board rows were ever right.** They ran with
  placement-delta feedback on and to their time budgets, which is the path
  #6314 showed could save an inconsistent board. Their files are gone. They
  are marked where they appear and are not used as baselines.
- **Scales between 1x and 1.5x.** A 1.25x sweep was started under the abandoned
  protocol (c) and has no usable rows. (True of #6304. #6312 has since run
  board 05 at 1.1x, 1.2x, 1.25x and 1.4x.)
- **Boards 06, 07, 08, 09.** 06 and 07 arrive with routed copper and are
  refused (by design, see above); 08 has no generated PCB; 09's `output/` board
  is already routed.
- **Anything above 1200 s.** In #6304's rows (unverified since #6314) both
  partial boards used their whole budget at both scales, and #6304 read the
  shape of that result (cells and path lengths both up, `BUDGET_STARVED` nets
  0-for-11) as giving no reason to expect a much larger budget to let the
  expanded board overtake. **#6312's committed rows do not support either
  premise on board 05 once feedback is off:** the 1x runs used 553, 786 and
  606 s of 1200 (the one 1x run with feedback at its default used 1188 s),
  and `PHASE_A`, `budget_starved` at 1x in all three, finished in 9 of the
  10 expanded rows. So that expectation no longer has support for board 05.
  A larger budget remains unmeasured, in #6304 and in #6312.
- **Non-uniform (single-axis) scaling.** The harness supports per-axis factors
  and refuses round outlines under them; it was not swept.
- **Run-to-run variance.** One run per row, on a loaded shared host. (The
  board-05 and chorus rows discussed here are #6304's, unverified since
  #6314.) The board
  05 and chorus differences there are two to four nets each from single pairs of
  runs, and are not individually significant. What carries the verdict is that
  four pairs — two boards under two protocols, one seed, so not independent
  samples — show no improvement in any of them, including the one pair where
  load favoured the expanded arm, and that the premise needed a large
  improvement, not parity. The board 03 result (0 vs 1 unfinished) is
  likewise a single pair; #6310 should re-run it before building on it.
  **A fixed seed is not run-to-run determinism.** In review of this note,
  board 01 was re-run at 1.5x under the `area` policy with the same `--seed
  42` and `PYTHONHASHSEED=0` and came back with 2 vias / 88.1 mm against the
  table's 3 / 89.7 (completion 3/3 both times; 1x and 2x matched the table).
  Two further re-runs of the same three rows on this branch reproduced the
  table exactly, so the variation is real but occasional, and its cause was
  not investigated. On a three-net board it changed one via; nothing here
  bounds what it does to a 37-net board at its budget limit, which is one
  more reason not to read the two-to-four-net differences as signal.
  (#6312 has since measured it on board 05: same-seed runs differ in copper
  on four or five nets and in which nets finish by at most one. It also found
  that a repeat is a cache replay unless `--no-cache` is passed, which the
  "reproduced the table exactly" re-runs above did not pass; whether they
  were replays was not checked.)

## Follow-ups filed

| Issue | What |
|---|---|
| #6310 | Probe: shrink board 03's rigid 1.5x route back to 1x once and see whether `USB_D+` survives. Decides whether a compaction pass is worth building at all. |
| #6312 | Measure `rigid` expansion on the boards that are partial at 1x. Done for board 05 (no improvement under protocol (a) at 1200 s; budget not varied); chorus has a clustering rule and was not routed. |
| #6327 | Stuck classifier: a power net with no zone is labelled `pour_discontinuous` (board 05 `VM`), so "signal nets unfinished" undercounts. Found in #6312. |
| #6328 | Auto-grid: board 05's `U2` fine zone flips between 0.05 mm and 0.005 mm when the part is translated. Found in #6312. |
| #6329 | `kct route --deterministic-budget --seed`: same-seed runs of board 05 differ on 4 nets, and seeds 42 and 43 can match exactly. Found in #6312. |
| #6330 | `kct route --timeout`: a deadline during placement-delta feedback can leave a `_partial` board with `snapshot_saved: false` and no `snapshot_state`. Found in #6312. |
| #6309 | Evaluate reporting over-capacity global-routing edges as a placement-spread trigger (refinement 3). |
| #6307 | Router: board 04 loses net `SWO` when its placement is spread to 2x. |
| #6306 | `kct route` refuses a 120 x 80 mm board with one LQFP-48; the auto-grid error omits `--max-cells` and advises enlarging the board. |
| #6308 | `kct route --timeout`: the sidecar's `unverified_output` names a copper-less board when the deadline lands in the routing stage. |

## Update (#6307): board 04 at uniform 2x

The 8/9 result (net `SWO` demoted after a via-vs-foreign-segment collision
with `NRST`) no longer reproduces. On current main, the harness command from
#6307 (`--grid-policy pinned --grid-mm 0.05 --scales 2.0 --modes uniform`,
seed 42, `--no-cache`) routes 9/9, 43/43 connections, 31 vias, 384.6 mm, with
0 `kct check` and 0 kicad-cli DRC errors, and the log contains no
via-vs-foreign-segment recovery or demotion. Intervening router changes
(lattice/pad-mask and recovery fixes since #6304) removed the collision; no
code change was made under #6307, so the specific mechanism that kept
recovery from separating `SWO` and `NRST` was not isolated. The row is now
guarded by `tests/test_board04_uniform_2x_6307.py` (slow). The fragility the
issue noted (a one-capacitor placement change flipping the result) is still
a property of this router; the monotonicity assumption in `auto_pcb_size`
remains unproven in general.

## Do plan overflow edges predict the stuck nets? (#6309)

Question: is the routing plan's `move_component` relief a usable trigger for
`router/placement_nudge.py`? The plan sidecar already ships (Epic #5510), so
the test is whether its overflowed edges coincide with the nets the stuck
classifier calls `PLACEMENT_BOUND` / `CONGESTION_SATURATED`. No new instrument
code was written; the inputs are `kct net-status --why --format json` beside
`<stem>.routing_plan.json`, and `scripts/routing_plan_fleet_table.py`.

Provenance: kct `86fd7afa1` (main), 2026-10-14, host loom-worker, scratch dir
`/tmp/b6309/05` (no tracked `output/` touched).

**Board 05.** Route argv (the `docs/reference/routing-plan.md` recipe, with
`--timeout` 1200 rather than 3600, and `--no-cache` so it is a real route, not
a replay; the route finished inside the budget):

```
PYTHONHASHSEED=0 kct route boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb \
  -o /tmp/b6309/05/bldc_controller_routed.kicad_pcb --auto-layers --starting-layers 4 \
  --max-layers 4 --manufacturer jlcpcb-tier1 --micro-via-in-pad-fallback --backend cpp \
  --seed 7 --timeout 1200 --per-net-timeout 60 --allow-unsafe-grid \
  --escape-corridor-reservation --skip-nets '+24V,+5V,+3V3,GND,PHASE_A,PHASE_B,PHASE_C' --no-cache
kct net-status /tmp/b6309/05/bldc_controller_routed.kicad_pcb --why --format json
kct net-status /tmp/b6309/05/bldc_controller_routed.kicad_pcb --strict --format json > net_status.json
python scripts/routing_plan_fleet_table.py /tmp/b6309/05 --sha 86fd7afa1
```

Plan: 2 overflowed edges, total overflow 2, `feasible: false`. Both edges are
bounded by `U2` on both sides, and each carries a `move_component` relief
(`U2`, dx 0, dy +2.0 mm, expected overflow 1) plus a deferred `swap_pins`.

| stuck class | stuck nets | cross an overflowed edge | argv |
|---|---|---|---|
| `placement_bound` | 1 (EN_B) | 1 (EN_B, edge 187-207) | rows above |
| `congestion_saturated` | 2 (CPH, GND_SENSE) | 0 | rows above |
| `budget_starved` | 3 (PHASE_A/B/C) | 0 | rows above |
| `escape_blocked`, `pour_discontinuous` | 0 | n/a | rows above |

| overflowed edges | count |
|---|---|
| total | 2 |
| crossed by at least one stuck net | 1 (187-207, via EN_B) |
| crossed by **no** stuck net | 1 (226-227; crossers PWM_B and EN_C both routed) |

Fleet-table row (strict opens: CPH, EN_B, GND_SENSE): recall 0.33 (1/3),
precision 0.50 (1/2). The `budget_starved` PHASE_A/B/C are nets the recipe
itself passes to `--skip-nets`; they are listed by `--why` but are not real
opens, so they only pad the "0 crossing" row.

**Chorus-test (#6578).** Measured on the operator's Mac, which has the sibling
`chorus` checkout. Provenance: kct `ee3bdb1c1` (main), chorus `3883176`,
2026-10-14, cpp backend built (`kct build-native`). The fixture is the tracked,
unmodified `hardware/chorus-test-revA/kicad/chorus-test-revA_v21_stripped.kicad_pcb`.
It was passed by absolute path because the relative `boards/external` symlink
dangles inside a Loom worktree. Output went to a scratch dir outside both repos.
Route argv: `route_chorus.py`'s main pass only (`R2_RECIPE_FLAGS`, seed 42),
with `--no-cache` added as for board 05. `route_chorus.py` itself was not run,
because it always continues into the completion and rescue passes:

```
CHORUS=~/GitHub/chorus/hardware/chorus-test-revA/kicad/chorus-test-revA_v21_stripped.kicad_pcb
PYTHONHASHSEED=0 kct route $CHORUS --output $SCRATCH/chorus/chorus-test-revA_routed.kicad_pcb \
  --manufacturer jlcpcb-tier1 --backend cpp --layers 4 --no-auto-layers \
  --micro-via-in-pad-fallback --deterministic-budget --per-net-iterations 1000000 \
  --placement-feedback --placement-feedback-budget 5 --iterations 50 --auto-fix \
  --auto-fix-passes 2 --timeout 1200 --seed 42 --no-cache
kct net-status $SCRATCH/chorus/chorus-test-revA_routed.kicad_pcb --why --format json
kct net-status $SCRATCH/chorus/chorus-test-revA_routed.kicad_pcb --strict --format json > net_status.json
python scripts/routing_plan_fleet_table.py $SCRATCH/chorus --sha ee3bdb1c1
# per-edge plan report (the run exits 9 before detailed routing):
PYTHONHASHSEED=0 kct route $CHORUS --output $SCRATCH/gate/chorus-test-revA_routed.kicad_pcb <same flags> --plan-gate
```

The result is not a usable measurement, for two reasons:

1. **The route collapsed.** It exited 1 with **0/37** signal nets routed, against
   the 30/51 recorded in `route_chorus.py`'s header. `J2` and `J4` now fail the
   OFF_BOARD placement gate, so 31 nets (14 signal nets, 3 pour nets, 14
   `unconnected-(J2-*)` stubs) were "placement-invalid, not attempted". The
   other 37 all ended in C++ `no_path`. The adaptive grid chose 0.0508 mm, and
   escape got only `U5` 10/28, `U8` 3/14 and `J2` 0/8. The stage deadline fired
   at about 777 s. With `--allow-offboard` added, the run exits at escape
   instead (`U8` 0/14). Filed as #6666.
2. **No plan sidecar was written.** The run hit the deadline and saved through
   the partial path, which writes `_partial.access_witness.json` and
   `_partial.search_failures.json` but no `<stem>.routing_plan.json`. As a
   result, `routing_plan_fleet_table.py` stops with "no *.routing_plan.json
   sidecar" and produces no fleet-table row. The issue's guidance was to record
   this rather than add instrument code, so it is recorded here. The plan was
   built: the log reports "overflow 3 on 3 edge(s)" after routing. The
   `--plan-gate` run reports only **2 edges, overflow 2**, so the pre-route gate
   plan and the in-route plan disagree by one edge. That run printed the
   per-edge detail below; the third edge was never printed.

| overflowed edge (`--plan-gate`) | nets crossing | relief |
|---|---|---|
| `U5` -> tile 25 (tiles 24-25, layer 0), demand 2.1 / cap 1 | DAC_CLK, Net-(U5-VNEG) | `swap_pins` deferred |
| tile 91 -> `U8` (tiles 91-92, layer 3), demand 1.1 / cap 1 | DAC_CLK_DIV2 | move `U8` -2.0 mm in x (overflow 1) |

The stuck classes come from `net-status --why` on the canonical output. The
first column covers the 37 attempted signal nets. The 14 placement-invalid
signal nets and the 5 pour nets are excluded, as board 05 excluded its
`--skip-nets`.

| stuck class | stuck nets (attempted) | cross a gate-plan overflowed edge |
|---|---|---|
| `placement_bound` | 8 (AUDIO_R, DAC_CLK, DAC_FLT, Net-(U5-CAPM), Net-(U5-OUTL), Net-(U5-OUTR), Net-(U5-VCOM/DEMP), SYNC_L) | 1 (DAC_CLK) |
| `budget_starved` | 29 | 2 (Net-(U5-VNEG), DAC_CLK_DIV2) |
| `congestion_saturated`, `escape_blocked` | 0 | n/a |
| `pour_discontinuous` | 5 (pour nets, excluded) | n/a |

The 2026-10-14 run gave no fleet-table row, so its recall and precision were
counted by hand from the gate plan (3/37 and a vacuous 2/2). The 2026-10-16
re-measure below replaces those numbers.

**Chorus-test re-measure (#6694).** Run on the operator's Mac after #6707 made
the partial-save exit write `<stem>_partial.routing_plan.json` for the board it
saves. Provenance: kct `8ce092f1f` (main), chorus `1582f69` (the v21 fixture is
byte-identical to `3883176`), 2026-10-16, cpp backend built (`kct build-native`,
build version 59), in a detached worktree with output in a scratch dir outside
both repos. The argv is the main pass that `route_chorus.py` runs today. Since
#6666 that recipe includes `--allow-offboard`, so J2/J4's 31 nets are attempted
and not skipped as placement-invalid. `--no-cache` is added as before:

```
PYTHONHASHSEED=0 kct route $CHORUS --output $SCRATCH/chorus/chorus-test-revA_routed.kicad_pcb \
  --manufacturer jlcpcb-tier1 --backend cpp --layers 4 --no-auto-layers \
  --micro-via-in-pad-fallback --deterministic-budget --per-net-iterations 1000000 \
  --placement-feedback --placement-feedback-budget 5 --iterations 50 --auto-fix \
  --auto-fix-passes 2 --timeout 1200 --allow-offboard --seed 42 --no-cache
P=$SCRATCH/chorus/chorus-test-revA_routed_partial.kicad_pcb
kct net-status $P --why --format json
kct net-status $P --strict --format json > $SCRATCH/chorus/net_status.json
python scripts/routing_plan_fleet_table.py $SCRATCH/chorus --sha 8ce092f1f
```

The route used its full budget and exited 124. The stage deadline fired during
detailed routing. The results summary gives 29/51 signal nets routed, 14
partial and 8 unrouted, which matches the 22 open nets below. The run then
saved through the partial path: `timeout.json` records
`stage: partial-save`, `status: partial` and the snapshot
`chorus-test-revA_routed_partial.kicad_pcb`. The canonical board was
quarantined as `*_timeout_unverified_*.kicad_pcb`. Beside the snapshot, the run
wrote `chorus-test-revA_routed_partial.routing_plan.json`, whose `source.pcb`
names the `_partial` board, and `net-status --why` auto-loaded it. The run
also reported 18 shorts (16 pre-existing in the input, #6693), and DRC was
skipped for lack of time left, so this board is a measurement input, not a
routed result.

| board | pcb | kct SHA | date | plan_s | relief_s | total_overflow | overflowed_edges | feasible | unrouted | unrouted_crossing | recall | precision | notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| chorus | chorus-test-revA_routed_partial.kicad_pcb | 8ce092f1f | 2026-10-16 | 0.06 | 1.95 | 9 | 6 | false | 22 | 3 | 0.14 (3/22) | 0.50 (3/6) | BOOT0, DAC_CLK_DIV2, I2S_DIN, I2S_LRCLK, LATCH, MCU/DBG_LED, MCU/DBG_LED2, NRST, Net-(C23-Pad1), Net-(C36-Pad2), Net-(D2-A), Net-(U5-VNEG), Net-(Y2-OUT), SCL, SDA, SPI_MISO, SPI_MOSI, SPI_NSS, SPI_SCK, SWCLK, SWDIO, SYNC_R |

`net-status --strict` reports 27 incomplete nets. The table drops the 5 the
plan files as `pour_skipped` (+3.3V, +3.3VA, +5V, GNDA, GNDD), leaving 22. Broken
down by `--why` class:

| stuck class | stuck nets | cross an overflowed edge |
|---|---|---|
| `congestion_saturated` | 12 | 2 (BOOT0 on 42-60, DAC_CLK_DIV2 on 74-92) |
| `placement_bound` | 4 (MCU/DBG_LED2, Net-(Y2-OUT), SCL, SWCLK) | 1 (SWCLK on 129-130) |
| `budget_starved` | 5 (LATCH, SDA, SPI_MISO, SPI_MOSI, SWDIO) | 0 |
| `escape_blocked` | 1 (SPI_SCK) | 0 |

| overflowed edge | demand / cap | nets crossing | crossed by a stuck net |
|---|---|---|---|
| 6-24 (`U5`) | 2.5 / 1 | I2S_BCLK, DAC_FLT | no |
| 6-7 (`U5`) | 3.5 / 2 | I2S_BCLK, Net-(LED4-K), Net-(U5-LDOO) | no |
| 24-42 (`U5`/`NT3`) | 2.5 / 1 | DAC_CLK, DAC_FLT | no |
| 42-60 (`NT3`,`U5` / `U9`) | 2.5 / 1 | BOOT0, DAC_CLK | yes (BOOT0) |
| 129-130 (`U8`) | 2.36 / 1 | SWCLK, UART_RX | yes (SWCLK) |
| 74-92 (`U8`) | 1.14 / 1 | DAC_CLK_DIV2 | yes (DAC_CLK_DIV2) |

### Verdict: no-go (board 05 and chorus-test measured)

- The only `PLACEMENT_BOUND` net does cross an overflowed edge, but that net is
  one of three real opens, and the plan misses the two `CONGESTION_SATURATED`
  ones entirely. Recall on real opens is 1/3 here and was 1/6 in the
  2026-09-21 table; a trigger keyed on plan overflow would act on a minority of
  the stuck nets.
- Half the overflowed edges (1 of 2) are crossed by no stuck net, so the
  trigger would also spread `U2` for a corridor whose nets all routed
  (precision 1/2 here, 1/2 in the earlier table; boards 02 and 04 overflowed and
  routed 100%).
- Both edges propose the same relief (`U2` +2 mm in y). A trigger would fire the
  same move twice and for one edge without any failing net; the existing per-net
  `PLACEMENT_BOUND` trigger already identifies EN_B.
- This agrees with the #6304 measurement that inter-footprint channels are
  nowhere near full on the fleet.
- Chorus-test does not change this. On the #6694 re-measure, recall is 0.14
  (3/22), lower than board 05's 0.33, and precision is 0.50 (3/6), the same as
  board 05's. 19 of the 22 open nets cross no overflowed edge. Only 1 of the 4
  `placement_bound` nets (SWCLK) crosses one. Its edge (129-130) carries a
  `move_component` relief for `U8`, so that is the one case where a plan
  trigger would have pointed `placement_nudge` at a placement-bound net. Three
  of the six overflowed edges, all around `U5`, are crossed by no stuck net.
  The no-go stands, and no build issue is filed.

Caveats: single run per board. Board 05 is not seed-deterministic (#3894,
#6329), so another run may differ by a net. The earlier table's six opens came
from a 3600 s run; this one has three. Both chorus runs hit
their stage deadline and are not reproducible to the net. The #6694 row was
measured on the deadline-saved `_partial` board with `--allow-offboard` (J2/J4
pads still sit outside Edge.Cuts, #6666), so it is not a manufacturable
configuration. The 2026-10-14 numbers describe the #6666 regression rather than
the plan-overflow question.

## The one shrink: board 03 rigid 1.5x back to 1x (Issue #6310)

**Verdict: no-go on a compaction pass.** The probe was run twice: first under
the grid engine (the issue's own command), then again on `origin/main` after
the lattice coupled-pair changes (#6517, #6534, #6542, #6568), under the
deterministic lattice protocol. Both runs give no-go. The lattice run also
removes the premise: at 1x, `USB_D+` already routes, so expansion has no net
to win back.

Harness: `scripts/research/shrink_experiment.py` (imports
`expand_route_experiment.py`). The lattice run's harness output is committed
as written, as `data/research/expand_route_shrink/03_lattice_*.json` (see
`data/research/README.md`). Routed boards stay in scratch, and the paths
inside the JSON point at that scratch tree.

### Lattice protocol on current main (the run that decides)

Tree: `origin/main` at `b9041b37d`. `main` moved while the runs were going
(the next merges include #6443's pair-rule changes to `kct check`). The rows
were not re-run on the newer tree.

Every route in this subsection used:

```
kct route --route-engine lattice --strategy basic --layers 4 --no-auto-layers \
    --deterministic-budget --per-net-iterations 1000000 \
    --no-placement-delta-feedback --seed 42 --no-cache --grid 0.05
```

The commands below regenerate the experiment. They do not reproduce these
values exactly: the boards are rerouted, and the wall-clock deadline depends
on host load.

```bash
W=/path/to/scratch   # outside the repo
P="--route-engine lattice --strategy basic --layers 4 --no-auto-layers \
   --deterministic-budget --per-net-iterations 1000000 --no-placement-delta-feedback"
ERE=scripts/research/expand_route_experiment.py
# Step 1: one command per row (scale/mode -> file)
uv run python $ERE route --boards 03 --grid-policy pinned --grid-mm 0.05 --seed 42 \
    --timeout 900 --extra "$P" --scales 1.0 --modes rigid \
    --work-dir $W/a --json 03_lattice_1x_s42.json
#   --scales 1.5 --modes rigid   -> 03_lattice_rigid15_s42.json   (--timeout 900)
#   --scales 1.5 --modes uniform -> 03_lattice_uniform15_s42.json (--timeout 900)
#   --scales 1.5 --modes rigid   -> 03_lattice_rigid15_s42_rep.json (--timeout 2400)
#   --scales 1.5 --modes rigid   -> 03_lattice_rigid15_s42_rep_t900.json (--timeout 900)
# Steps 2-4: shrink the first rigid 1.5x route
uv run python scripts/research/shrink_experiment.py \
    --src boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
    --routed $W/b/03-rigid-1.5-pinned/usb_joystick_routed.kicad_pcb \
    --scaled-input $W/b/03-rigid-1.5-pinned/usb_joystick.kicad_pcb \
    --scale 1.5 --seed 42 --extra "$P" --timeout 2400 \
    --out-dir $W/shrink --json 03_lattice_shrink_s42.json
#   same with --timeout 900 -> 03_lattice_shrink_s42_t900.json (repair hit the deadline)
```

The repair route in the shrink step ran under the same protocol. The host is
an 8-core machine whose load average was between 13 and 53 during the runs,
because other agents were running.

**Step 1: the 1.5x rigid route, and the 1x baseline.** Source:
`03_lattice_1x_s42.json`, `03_lattice_uniform15_s42.json` and
`03_lattice_rigid15_s42.json` (`rows[0]`).

| Placement | Nets (all) | Signal nets unfinished | `kct check --mfr` errors | `kicad-cli` errors (unconnected) | Pours split | Vias | Wire mm |
|---|---|---|---|---|---|---|---|
| 1x | 26/27 | **0** (`USB_D+` routed, 1 via) | 2 (diffpair skew, diffpair continuity) | 0 (1) | `VBUS` | 73 | 941.5 |
| 1.5x uniform | 27/27 | 0 | 2 (diffpair skew, diffpair continuity) | 0 (0) | none | 62 | 1345.6 |
| 1.5x rigid | 25/27 | 0 | 1 (diffpair continuity, 46.1 % < 70 %) | 0 (3) | `GND`, `VCC` | 69 | 1250.1 |

- **The premise does not hold under this protocol.** `USB_D+` is not lost at
  1x. All 24 signal nets route at every scale. None of the three rows is
  clean on `kct check`, and the reason is the same in all three: `USB_D` is
  only partially coupled. Its connector leg `J1.B6->U2.3` falls back to an
  uncoupled route (`pair-no-path` at 1x and uniform 1.5x, `pair-leg-keepout`
  at rigid 1.5x). Pair continuity is 19.4 %, 38.2 % and 46.1 % against a
  70 % threshold. These coupling details and percentages come from each
  row's `route.log`, which is not committed; the JSON records only the rule
  counts. Expansion raises the continuity figure but does not couple
  the leg. That leg is tracked by #6378 and #6444. The 1x `VBUS` pour split
  is tracked by #6589.
- **The rigid row reproduces.** A second run with a 2400 s budget
  (`03_lattice_rigid15_s42_rep.json`) produced copper identical to the first
  run, apart from UUIDs. That was checked with a diff of the two routed
  boards, which are not committed. The JSON rows agree on every graded
  figure. A second run with the 900 s budget
  (`03_lattice_rigid15_s42_rep_t900.json`) did not: it hit the wall-clock
  deadline at 907.8 s (load average about 41) and finished with 1 signal net
  open and 212 `kicad-cli` errors. `--deterministic-budget` makes each net's search reproducible, but
  the overall `--timeout` is still wall-clock. On a loaded host, a deadline
  cut-off can therefore look like a different route.
- **No footprint moved during the route.** Feedback was off. Every footprint
  in the routed board sits where the scaled input put it. An earlier version
  of the census reported U1, U2, R3 and R4 as moved, but that was only
  rotation spelled `270` versus `-90`; the census now compares rotations
  modulo 360.

**Step 2: the inverse map (acceptance criterion 1).** Source:
`03_lattice_shrink_s42.json` (top level, `s0_naive`, and
`s0_naive.displaced_detail` for the per-net counts and histogram). The routed board has
825 segments, 69 vias and 48 connected copper components. 200 vertices
translate with their cluster, and 1519 scale by 1/1.5 about the outline
centre (148.5, 87.5).

- **Trace ends on a pad at 1.5x: 451. Displaced from that pad after the
  inverse map: 298 (66 %), on 19 nets**, including `USB_D+` (28 ends) and
  `USB_D-` (26 ends). The miss from the pad centre has a median of 1.48 mm and
  a maximum of 4.35 mm. By rounded millimetre: 0: 10, 1: 141, 2: 46, 3: 94,
  4: 7. Only 10 of the 298 miss by 0.5 mm or less.
- The naive board grades 6/27 nets, 18 signal nets open, 350 `kct check`
  errors and 709 `kicad-cli` errors (41 unconnected).

**Step 3: repair with existing machinery.** Source:
`03_lattice_shrink_s42.json` (`s1_snap`, `s2_reroute`), and
`03_lattice_shrink_s42_t900.json` for the attempt that hit the deadline.

- **Snap.** Each pad-attached vertex translates with its pad's footprint
  (394 vertices), and free vertices stay scaled. All 298 displaced ends land
  back on their pads (0 of 451 displaced), so this is the only repair that
  happens automatically. All 24 signal nets are connected again, but the
  board has 413 `kct check` errors and 817 `kicad-cli` errors (199
  `shorting_items`, 246 `clearance`, 135 `hole_clearance`, 48
  `tracks_crossing`, ...). Every one of the 27 nets except `USB_CC2` is named
  in a violation.
- **Rip and re-route.** Every net named in a `kicad-cli` violation or left
  stuck was ripped: 26 of 27 nets, 878 of 894 segments and vias. The 16
  items that survived are all on `USB_CC2`. `kct route --preserve-existing` then
  re-routed under the same protocol. The first attempt hit the 900 s deadline
  (during zone fill, per its route log) and wrote no board. With a 2400 s budget it finished in
  1377 s.

**Step 4: grade (acceptance criterion 2).** Source: the `grade` objects in
`03_lattice_shrink_s42.json`. The comparison row is from
`03_lattice_1x_s42.json`.

| Stage | Nets (all) | Signal nets unfinished | `kct check --mfr jlcpcb` | `kicad-cli pcb drc --refill-zones` (unconnected) |
|---|---|---|---|---|
| naive inverse map | 6/27 | 18 | 350 | 709 (41) |
| snapped | 24/27 | 0 (3 pours split) | 413 | 817 (11) |
| snapped, 26 nets re-routed | 26/27 | 0 (`VBUS` pour split) | 2 (diffpair skew, diffpair continuity) | 0 (1) |
| *fresh 1x route, for comparison* | 26/27 | 0 (`VBUS` pour split) | 2 (same two rules) | 0 (1) |

The `kct check` counts for the first two rows are graded without the
board's net-class map, so the diffpair rules were inactive for those rows
only. The rows are far from clean either way.

The repaired board matches a fresh 1x route: the same nets, the same two
`kct check` rules, the same `VBUS` split, 73 vias in both, and 929.7 mm of
wire against 941.5 mm. `USB_D+` is present, with 1 via, but it was
re-routed, not kept.

**Go / no-go (lattice protocol): no-go.** Against the issue's rule:

- `USB_D+` was not kept by the shrink. Snapping put its 28 displaced ends
  back on their pads, but the snapped board shorts. The only clean board
  re-routed `USB_D+` along with 25 other nets.
- The repair was not limited to trace ends at pads. It re-routed 26 of 27
  nets (96 %), which amounts to routing again at 1x, and the result is the
  1x baseline.
- There was nothing to win back. Under this protocol the 1x route already
  completes `USB_D+`.

### First run: grid engine, the issue's command (superseded by the lattice run above)

**Provenance: no committed file backs the numbers in this subsection.**
The step-1 harness JSON and the `shrink_experiment.py --json` output from this
run were written to `/tmp/ers-03/` on the worker host and are gone. Every
figure below, including route lengths, via counts and the miss-distance
histogram, was read from that run's output and cannot be re-read from the
repo. Treat them like the "not committed" rows above (#6304). The lattice
run above replaces them, and its data is committed.

This run used the grid engine, `--seed 42`, the pinned 0.05 mm grid and a
900 s budget, with placement-delta feedback at its default (on), on a
shared, loaded host.

#### Step 1: the 1.5x rigid route did not reproduce as complete and clean

The command in the issue, re-run (`--no-cache`):

| Placement | Nets (all / signal) | `kct check` errors | `kicad-cli` DRC errors | Unfinished |
|---|---|---|---|---|
| 1x | 26/27, 23/24 | 2 (1 connectivity, 1 diffpair continuity) | 0 | `USB_CC1` (`placement_bound`) |
| 1.5x uniform | 26/27, 24/24 | 4 | 10 (`tracks_crossing`) | `GND` (`pour_discontinuous`) |
| **1.5x rigid** | 26/27, 24/24 | **2** (diffpair skew 1, continuity 1) | **2** (`tracks_crossing`) | `GND` (`pour_discontinuous`) |

Differences from the issue's table: the rigid route is 24/24 on signal nets
but is not "27/27 including pours" and not clean on either engine, and the
net stuck at 1x on this run is `USB_CC1`, not `USB_D+` (`USB_D+` routes at 1x
here, with 2 vias). So the case the issue singles out as "worth trying"
is, in this run, a route that already has errors before any shrink, against a
1x baseline that loses a different net. Single runs on both sides, so this
shows the earlier table is not stable, not that it was wrong.

#### Step 2: the inverse map (acceptance criterion 1)

Footprints go back to their 1x positions (per-footprint delta between the 1x
board and the routed board, all 38 footprints had moved). Copper: a connected
component that touches pads of exactly one cluster translates with it (222
vertices); every other vertex scales by 1/1.5 about the outline centre
(1323 vertices). The board holds 737 segments, 71 vias, 56 components.

- **Trace ends on a pad at 1.5x: 303. Displaced from that pad after the
  inverse map: 181 (60 %), on 19 of the board's nets**, including `USB_D+`
  (14 ends) and `USB_D-`. Miss distance from the pad centre: median 1.84 mm,
  max 4.56 mm; histogram by rounded mm 0:2, 1:62, 2:64, 3:42, 4:9, 5:2.
  Only 2 of the 181 miss by 0.5 mm or less.
- The naive 1x board grades 7/27 nets connected, 340 `kct check` errors, 644
  `kicad-cli` errors.

The count is large because a pad's offset from its cluster centroid scales by
1/1.5 under the inter-cluster map but not under the cluster translation; any
pad more than a few tenths of a millimetre from the centroid is missed.

#### Step 3: repair with existing machinery (acceptance criterion 1, second half)

- **Snap (pad-attached vertices translate with their pad's cluster, free
  vertices stay scaled):** all 181 displaced ends land on their pads
  (0 displaced of 303), and 24/27 nets reconnect. That is the only "automatic"
  repair, and it is a coordinate rule, not `drc_nudge`. It trades the
  displacement for geometry: 342 `kct check` errors (99 `clearance_pad_segment`,
  98 `clearance_segment_via`, 68 `clearance_segment_segment`, ...) and 648
  `kicad-cli` errors, with 24 of 27 nets named in a violation (25 entries, counting kicad-cli's
  `<no net>` bucket). Segments that
  now join a moved end and a scaled end are no longer on the routing grid,
  at 45 degrees or at clearance; `drc_nudge` moves segments by tenths of a
  millimetre and cannot repair violations of this size, and it only runs
  inside `kct route`, where it is handed routes the router created.
- **Rip and re-route what is still violating:** 24 of the 27 nets with copper
  (791 of 808 segments and vias) were ripped, and
  `kct route --preserve-existing --grid 0.05` routed them again.

#### Step 4: grade (acceptance criterion 2)

| Stage | Nets (all) | Signal nets unfinished | `kct check --mfr jlcpcb` | `kicad-cli pcb drc --refill-zones` |
|---|---|---|---|---|
| naive inverse map | 7/27 | 17 | 340 | 644 |
| snapped | 24/27 | 0 (3 pours discontinuous) | 342 | 648 |
| snapped, 24 nets re-routed | 26/27 | 1 (`USB_CC1`, `placement_bound`) | 2 (connectivity, diffpair continuity) | 0 (1 unconnected) |

The final board is the same result as the 1x baseline row of Step 1 (26/27,
`USB_CC1` open, 2 `kct check` errors, 0 `kicad-cli` errors; 710 mm and 71 vias
against 708.5 mm and 73 vias). `USB_D+` is present and clean, but it was
re-routed, not preserved.

#### Go / no-go (grid engine)

The issue's criteria: go needs the shrunk board to keep `USB_D+`, be clean on
both engines, and confine repair to trace ends at pads. No-go if repair
re-routes a large share of nets.

- Repair is not confined to ends at pads: 60 % of pad ends were displaced and
  the only repair that avoided re-routing left 648 `kicad-cli` errors.
- The fix that works re-routes 24 of 27 nets (89 %), which is "route again at
  1x", and its result equals the 1x baseline (so it also loses a net, `USB_CC1`).
- The rigid 1.5x route that would be shrunk was itself not clean (2 `kct
  check`, 2 `kicad-cli` errors, `GND` fill split).

**No-go.** Do not build the compaction pass, the alternating-axis loop or a
`kct route` flag on this evidence. Limits: one board, one seed, one run per
step; the inverse map is the simplest reading of the issue (cluster by
connected component, pad-attachment by pad bounding box). A smarter shrink
could displace fewer ends, but the 648-error snapped board and the 89 %
re-route share show the cost is in the inter-cluster copper, which no end-repair
addresses. Re-running Step 1 with other seeds would show whether the 1.5x rigid
route is ever complete and clean here; it was not done.
