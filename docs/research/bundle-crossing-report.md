# Bundle crossing report: measured hit and false-alarm counts (Issue #6339)

`kct net-status <pcb> --crossings` (module
`src/kicad_tools/router/bundle_crossings.py`) is a read-only, pre-route report.
It does not change routing. Layer hints, an in-route enclosure check and new
stuck-classifier classes (items 2-4 of the issue) are not built here.

## What it reports

* **Bundles.** Part pairs joined by three or more two-terminal signal nets,
  split by (side of part A, side of part B) so each group is one pin row facing
  one pin row. Per bundle: both pin orders, inversions per candidate entry side,
  the minimum, the inverted pairs at the minimum, and the colour count of that
  conflict graph against the signal-layer count. Inversions come from
  `bundle_river.compute_facing_row_inversions`; colouring from
  `bundle_river._min_graph_colouring` (exact up to 10 nodes, clique and greedy
  bounds above that).
* **Hub columns.** Pin columns with three or more signal nets that have a pad on
  another part: exit direction per pad (left/right/up/down toward the net's other
  pads) in column order, and the directions that another direction interleaves.
* Same-axis rows have one natural entry; different-axis (L-shaped) rows are
  scored both aligned and mirrored (the bundle entering from the other end of the
  destination row) and the minimum is taken. Which end is the natural one is not
  decided by the tool (chirality), so for L-shaped bundles the minimum is a lower
  bound.

## Method

Main SHA: `b9041b37df7393afc68625ee53dd5aee02cfaff9` (plus this change).

```
uv run python scripts/measure_bundle_crossings.py
```

For each `boards/0*/output/<name>.kicad_pcb` the script runs the report with the
board's own signal-layer count, and takes the unfinished set from the committed
`<name>_routed.kicad_pcb` (incomplete or unrouted signal nets per
`NetStatusAnalyzer`). A net is *flagged* when it appears in an inverted pair or
in an interleaved hub group. *Hit* = flagged and unfinished, *false alarm* =
flagged and complete, *miss* = unfinished and not flagged.
`git status --porcelain boards/` was clean afterwards.

## Results

| board | bundles | with forced crossings | interleaved hubs | flagged nets | unfinished (committed routed) | hits | false alarms | misses |
|---|---|---|---|---|---|---|---|---|
| 00-simple-led | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 01-voltage-divider | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 02-charlieplex-led | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 03-usb-joystick | 0 | 0 | 1 | 7 | 0 | 0 | 7 | 0 |
| 04-stm32-devboard | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 05-bldc-motor-controller | 2 | 1 | 2 | 17 | 0 | 0 | 17 | 0 |
| 06-diffpair-test | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 07-matchgroup-test | 6 | 5 | 2 | 32 | 0 | 0 | 32 | 0 |
| 09-usbc-pd-power | 0 | 0 | 1 | 3 | n/a (no committed routed board) | n/a | n/a | n/a |
| external/chorus-test-revA | skipped: path is empty in this checkout | | | | | | | |

### Reading the table honestly

* **Every committed routed board in the fleet is complete** (0 unfinished
  signal nets), including boards 03 and 05, which the issue describes as leaving
  nets unfinished. The unfinished sets quoted in the issue are not in the
  committed outputs, and board 05's current unfinished set was not re-measured
  (that needs a routing run, which this change deliberately does not do). So the
  **hit rate cannot be measured from committed data**: hits are 0 because the
  unfinished sets are empty, not because the report missed anything. Misses are
  likewise 0 vacuously.
* The "false alarm" column therefore means "flagged, yet the committed board
  routed it". A flagged bundle is a forced crossing, which routers resolve with
  vias; it is not a prediction of failure. The counts say the report flags a lot
  of nets that do route (board 07: 32), so a flag alone is a weak failure
  predictor. It is a geometry finding.
* The three boards with zero flagged nets are boards with no 3-net bundles and no
  interleaved hubs. Board 04 has none of either.
* Board 07 (a deliberately reversed DDR-style layout) is flagged most heavily,
  and at 2 signal layers four of its six bundles are over-sized (conflict graph
  needs more than 2 colours). At the board's own 6 layers none is over-sized in
  the default run.
* The hub rule is the noisiest signal: it flags any column where exit directions
  alternate (for example `RESET>right` between `down` exits on board 03's U1), and
  such columns are common and often benign.

## Board 05 (acceptance check from the issue)

At 2 signal layers (`--signal-layers 2`):

* Bundle `U1.down -> U2.right` (4 nets): U1 order `PWM_B PWM_C EN_B EN_C`, U2
  order `PWM_B EN_B PWM_C EN_C`. Inversions: aligned entry 1, mirrored entry 5,
  minimum 1, the single inverted pair is `PWM_C/EN_B`. This is the transposition
  from the issue, found for an L-shaped bundle (U2 right column vertical, U1
  bottom row horizontal).
* Hub `U2.left`: exit order `CPL>left CPH>up VCP>left PHASE_A>left GND_SENSE>down
  GND_SENSE>down PHASE_B>left PHASE_C>left GND_SENSE>down IREF>down SENSE_IN>down`.
  `PHASE_B`/`PHASE_C` (left) are interleaved with `GND_SENSE` (down), matching the
  issue.
* `DRIVER_SLEEP` and `nFAULT`, which also run U2 to U1's bottom row, are not
  in that bundle because they have pads on a third part (3+ part nets are
  skipped and counted, not bundled).

## Limits and follow-ups

The measurement does not support building the layer hint, the in-route
enclosure check or new classifier classes yet. The committed boards give no
unfinished nets to predict, and the false-alarm volume says a flag needs
routed-run evidence (for example the unfinished set of a fresh 1x run) before it
drives routing. No follow-up issue is filed from these numbers.
