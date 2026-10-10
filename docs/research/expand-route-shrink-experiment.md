# Does routing an expanded placement and shrinking it back help? (measured: not on the boards that need it)

Issue #6304. The proposal: place the board, scale the placement ~2x so the
router has room, route, then shrink back ~1% at a time on alternating axes,
repairing traces at each step. It is attractive as an *anytime* algorithm — a
fully-routed, DRC-clean board in hand at every step, with only area given up.

**Verdict: no-go on building the shrink loop (Phases 3–4). It is not built.**
The idea rests on one premise — *a board that is partial at 1x becomes
complete when expanded* — and on the boards that are actually partial, the
premise fails:

- **Both boards that are partial at 1x got worse when expanded.** Board 05
  went from 4 unfinished signal nets to 6 at 1.5x; chorus-test from 29 to 31.
  Neither could be measured at 2x: the router refuses one and cannot finish
  the other's 1x board in budget.
- **Expansion costs the one resource those boards are short of.** Both are
  budget-bound at 1x, and at 1.5x the routing grid is 1.5–1.6x larger.
- **`kct route` as shipped refuses the 2x boards that matter.** Its auto-grid
  is cell-budgeted, so a 4x-area board gets a coarser grid, which fails the
  router's own `clearance/2` safety rule on a board with a fine-pitch part
  (boards 04 and 05 here; board 02, which has none, routes).
- **More room is not reliably better.** Board 04 routes 9/9 at 1x and 8/9
  uniformly spread to 2x on the same grid. Board 03 is complete at 1.5x and
  broken at 2x.

**There is one positive, and it should not be lost in the verdict.** On board
03 with an explicit 0.05 mm grid, net `USB_D+` is unfinished at 1x and the
board is fully complete — 27/27 nets, zero errors on both DRC engines — when
its placement is expanded 1.5x with decoupling held rigid to its IC. That is
exactly what the proposal predicts. It is one net on one board, in a grid
configuration where the *default* grid already routes that net at 1x, so it
does not change the verdict; but it is a real instance of the premise holding,
and a single shrink of that one route is the cheapest possible test of the
rest of the idea. That probe is filed as #6310.

Phase 2 was run on everything, because it answers the question the issue
flagged as the main risk: would shrinking be geometry, or re-topology? Across
34 expanded routes, **8 of 3,820** inter-footprint gaps carrying copper hold
more than they could at 1x (four on each of chorus's two 1.5x routes, nowhere
else). Shrinking would be almost entirely geometry. The idea fails at step 3 (route), not step 4
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
  conservative: board 04 forms one two-part cluster out of 17 footprints.

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
- `--seed 42` and `PYTHONHASHSEED=0` in every run. **One run per row.**
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
  completion was cut off by the deadline. Whatever expansion buys this board, it buys at 1.5x
  and loses again by 2x.
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
load), otherwise worse, and never better** — on four independent 1x/1.5x
pairs. The premise needed a large
improvement to be worth a shrink loop, and there is no improvement.
(Host load favoured the 1x arm in two of the four pairs, the expanded arm in
one, and neither in one.)

**(c) Explicit `--grid`, 900 s** — abandoned. Both boards' 1x runs died at the
hard deadline in a late stage (05: 10 unfinished; chorus: 48), the sweep was
stopped, and the numbers are noise for the reason given in trap 2. Mentioned so
nobody repeats the protocol; not evidence either way.

**Not measured:** board 05 and chorus at 2x (the router refuses 05 under
`default` and `area`, and the only protocol that could route it — explicit
grid — could not finish even the 1x board in budget); and either partial board
in `rigid` mode. Given what `rigid` did for board 03, the second is the more
important omission.

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
- `BUDGET_STARVED`: 0 of 11 under (a), 0 of 12 under (b). Expected — these nets
  are short of search budget, and a larger board makes every search longer.
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

(Of the rows not shown — every other scale and mode, the `area`-policy runs,
and protocol (b) — all are zero except chorus at 1.5x under protocol (b), which
has the same four. In total: 34 expanded routes, 3,820 loaded gaps, 8 over
capacity.)

Every 1x row is zero, as it must be for a board with no clearance errors
between its own traces — that is the metric's control. The four chorus gaps
are sub-0.5 mm slots between adjacent parts (`C11`–`J2` is 0.18 mm wide at 1x)
that carry nothing at 1x and one trace each at 1.5x: exactly the "topology
found with free space that cannot exist at final size" the issue was worried
about. It is real, it reproduces across both chorus 1.5x routes, and it is
0.6% of chorus's loaded gaps.

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

## Why it mostly does not work

Three reasons, in decreasing order of how much of the result they explain.

1. **Uniform scaling preserves exactly the thing that makes a net unroutable.**
   An affine map leaves the placement's topology untouched: which pins face
   which, the cyclic order of nets around every part, which nets must cross.
   A net that is stuck because its partner pad is on the far side of a bundle
   it has to cross is stuck at every scale; it needs a via, a layer, or a part
   rotated — not millimetres. Scaling only relieves *channel capacity*, and
   Phase 2 says channel capacity is not binding on boards 05 and chorus.
2. **Pin-field geometry does not scale, and that is where the density is.**
   The proposal anticipated this for `ESCAPE_BLOCKED` nets specifically. It is
   more general: a 0.5 mm-pitch QFP presents the same escape problem on a
   60 mm board and a 120 mm one.
3. **The router is budget-bound on precisely the boards that are partial, and
   expansion spends the budget.** Cells grow as s² at fixed pitch (board 05:
   3.93 M → 5.79 M estimated at 1.5x; 4x at 2x), paths are s times longer, and
   A\* expansions grow with both. Under protocol (a), 11 of the 33 unfinished
   signal nets at 1x across the two partial boards are `BUDGET_STARVED` before
   anything is expanded, and none of them completes.

Board 03 is the exception that fits the rule: one stuck net, a small board
that finishes well inside its budget at 1.5x, and — the part `rigid` supplies —
a placement change that is *not* purely affine, because the clusters keep
their internal geometry while the space between them opens. Reason 1 applies
to `uniform`; it applies less to `rigid`. That is the most interesting thing
this experiment did not get to test on the hard boards.

The anytime property — a complete, clean board at every step — was the
proposal's main attraction, and it depends on step 3 delivering a complete,
clean board to start from. On the boards that need help, it does not.

## Go / no-go

**No-go on Phases 3–4 as specified.** No shrink-loop prototype is built, and no
flag is added to `kct route`. One narrow probe is filed (#6310) instead of a
prototype.

| Question from the issue | Answer |
|---|---|
| Do boards that are partial at 1x reach 100% expanded? | No. 0 of 2 under the shipped router; both lose nets. |
| Does anything complete expanded that does not at 1x? | Yes, once: board 03 on an explicit grid, rigid 1.5x (27/27, 0 DRC both engines). The default grid already routes the net concerned at 1x. |
| Do boards that are complete at 1x stay complete? | Not reliably: board 04 loses a net at uniform 2x; board 03 loses two at rigid 2x. |
| Do `ESCAPE_BLOCKED` nets fail to benefit, as predicted? | Untestable — none exist at 1x on this fleet. |
| Do `PLACEMENT_BOUND` nets benefit, as predicted? | On board 03, yes (1 of 1). On 05 and chorus, about one in four, with more nets newly stuck than freed. |
| Is shrinking geometry or re-topology? | Geometry: 8 of 3,820 loaded gaps over 1x capacity, all on chorus. |
| Minimum feasible scale per board, both DRC engines? | Not measured — it needs the shrink step. Only board 03 has an expanded starting point (rigid 1.5x) that is better than its 1x run, and only against the explicit grid. |

What would change the verdict, in order of cost:

1. **#6310 says go** — board 03's rigid 1.5x route survives a single shrink to
   1x with `USB_D+` intact and both DRC engines clean, with repair confined to
   trace ends at pads.
2. **`rigid` mode helps a board that is partial under the shipped router.** Not
   measured here. One run each of board 05 and chorus at rigid 1.25–1.5x under
   protocol (a), on an unloaded host, would settle it.
3. **A board on which inter-footprint channel capacity is the binding
   constraint** — the `gaps` subcommand reporting at- or over-capacity gaps on
   a 1x partial route. None of the seven boards here is one. A dense two-layer
   board with long parallel buses between rows of parts is where to look.

## Refinements the data suggests

Ordered by how directly the measurements support them.

1. **If anything is scaled, keep clusters rigid — and treat that as the idea,
   not a detail of it.** Every positive in this experiment is a `rigid` row:
   board 03 complete at 1.5x, its pour discontinuity cleared under both grids,
   board 04 not regressing at 2x. `uniform` is electrically wrong and never
   won a comparison. The mechanism is plausible too: a rigid-cluster spread is
   not affine, so it can change which channels exist, where a uniform scale
   only widens the ones already there.
2. **Expand a little, not a lot.** Board 03's gain is at 1.5x and is gone by
   2x; the s² grid cost is the reason. The proposal's "~2x" is past the useful
   range on every board measured. If this is revisited, sweep 1.1–1.5x.
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
   Global expansion pays the full s² cost to relieve gaps that were never
   tight.
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

- **Board 05 and chorus at 2x**, and either in `rigid` mode. See "Boards that
  are partial at 1x". The second is the gap most likely to matter.
- **Scales between 1x and 1.5x.** A 1.25x sweep was started under the abandoned
  protocol (c) and has no usable rows.
- **Boards 06, 07, 08, 09.** 06 and 07 arrive with routed copper and are
  refused (by design, see above); 08 has no generated PCB; 09's `output/` board
  is already routed.
- **Anything above 1200 s.** Both partial boards used their whole budget at
  both scales. A much larger budget might let the expanded board catch up; the
  shape of the result (cells and path lengths both up, `BUDGET_STARVED` nets
  0-for-11) gives no reason to expect it to overtake.
- **Non-uniform (single-axis) scaling.** The harness supports per-axis factors
  and refuses round outlines under them; it was not swept.
- **Run-to-run variance.** One run per row, on a loaded shared host. The board
  05 and chorus differences are two to four nets each from single pairs of
  runs, and are not individually significant. What carries the verdict is that
  four independent pairs across two protocols show no improvement in any of
  them — including the one pair where load favoured the expanded arm — and
  that the premise needed a large improvement, not parity. The board 03 result (0 vs 1 unfinished) is
  likewise a single pair; #6310 should re-run it before building on it.

## Follow-ups filed

| Issue | What |
|---|---|
| #6310 | Probe: shrink board 03's rigid 1.5x route back to 1x once and see whether `USB_D+` survives. Decides whether a compaction pass is worth building at all. |
| #6309 | Evaluate reporting over-capacity global-routing edges as a placement-spread trigger (refinement 3). |
| #6307 | Router: board 04 loses net `SWO` when its placement is spread to 2x. |
| #6306 | `kct route` refuses a 120 x 80 mm board with one LQFP-48; the auto-grid error omits `--max-cells` and advises enlarging the board. |
| #6308 | `kct route --timeout`: the sidecar's `unverified_output` names a copper-less board when the deadline lands in the routing stage. |
