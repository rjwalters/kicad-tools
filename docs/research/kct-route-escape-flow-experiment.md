# Does joint min-cost-flow escape assignment help `kct route`? (measured: no; it never beats the default, and capacity never binds)

Issue #6352, candidate 1 of epic #5947. Companion to
[`kct-route-ripup-experiment.md`](kct-route-ripup-experiment.md), whose format
this note follows.

**Verdict: decline. The prototype stays in only as an opt-in experiment.**
Across five boards, the joint min-cost-flow assignment (arm C) never routed
more connections than the default without corridors (arm A):

- **Boards 01, 03, 04 and 06a.** C's routed copper is byte-identical to the
  existing greedy corridor planner's (arm B). It also matches A on completion
  and vias, with wirelength within 0.05 mm.
- **Board 05**, the dense board #4474 was built for, is the only board where
  the arms differ. Greedy corridors (B) *regress* it: kct exits 4 (partial
  routing plus clearance violations) instead of 2, with 127 vias instead of
  107 and 1110 mm of wirelength instead of 1038 mm. That result reproduced
  byte-for-byte on a re-run. C avoids that regression and lands back on A's
  numbers: exit 2, 131/142 connections and 28/37 nets, the same as A, with
  106 and 108 vias and 1040 and 1035 mm of wirelength over two runs. **C
  undoes B's harm, but it does not improve on routing without corridors.**

The treatment did fire. All 9 package plans on board 05 (3 parts, each planned
in 3 escalation passes) and every plan on the other boards came from the flow,
and none fell back. On boards 03, 04 and 05 the flow reserved different cells
or layers than greedy did. Three findings bound what any assignment strategy
can show on this fleet:

1. **The capacity constraint never binds.** No plan on any board filled an
   exit to capacity. Peak exit utilisation was 0.50 (board 05, U3). With no
   saturated exit, min-cost max-flow is just the cheapest exit for each pin
   taken independently. The *joint* part of the TraceMaker idea, resolving
   pins that compete for one channel, is never exercised. Board 05's C-vs-B
   difference therefore comes from different corridor geometry and layer
   choice, not from joint allocation.
2. **The SOFT corridor reservation is close to inert, or harmful.** B against
   A changes one or two segments on boards 04 and 06a, nothing on 01 and 03,
   and regresses 05. A better assignment can at best cancel that effect.
3. **The access-loss failure class did not occur.** No terminal on any arm of
   any board ended with an empty access set (`stranded = 0` in every access
   witness). Board 05's unrouted terminals all still had legal first moves
   ("search refused legal copper"). With the pad-access invariant on by
   default, the boxed-in-pad failure this experiment targets is absent from
   the current fleet.

This is a valid null result for the hypothesis. "Not measured" lists what
would reopen it.

Tool: `scripts/research/escape_flow_experiment.py`. Implementation under test:
`src/kicad_tools/router/escape_flow.py` (solver and cost model) and the
`assignment="flow"` strategy of `EscapeCorridorPlanner`
(`src/kicad_tools/router/escape_corridor.py`).

## Licence boundary

TraceMaker is GPL-3.0-or-later and `copyleft-ideas-only` in the ecosystem
registry (`kct ecosystem show tracemaker`). Only its one-line description was
used ("min-cost-flow escape-corridor reservation"). No TraceMaker source was
read for this work. The solver is the textbook successive-shortest-path
algorithm (Dijkstra with Johnson potentials). The cost and capacity model
below is this repository's own hypothesis.

## What was built

| Piece | How |
|---|---|
| Allocation model | `source -> pin -> exit (face, layer) -> sink`. Each pin carries 1 unit. Each exit's capacity is the number of tracks that fit across that face on that layer. The result is a max flow, and the cheapest among max flows. |
| Capacity (hypothesis) | Probe a line one pad half-extent plus one track pitch (`trace_width + trace_clearance`) outside the pad-centre bbox, along the face. Count grid cells that are free, or blocked only by the package's own nets. Capacity is `floor(free_length / pitch)`. Off-board probe points count as blocked. |
| Cost (hypothesis, integer µm) | The in-package leg to the exit point, multiplied by 2 for a foreign face (under the body), plus the leg from the exit to the destination. Add 1.0 mm when the exit layer is not the pad's own layer (SMD), and 2.0 mm × the normalised face congestion demand. Each extra pin on an exit adds a convex 0.05 mm, so tied layers share load. |
| Destinations | The same as greedy (`_destination_for`): the Kelvin root, then the centroid of the off-package pads, then outward from the package. |
| Determinism | Pins are sorted by `(ref, pin, net, x, y)` and exits by face (N, E, S, W) then layer. Arcs relax in insertion order, and Dijkstra pops `(dist, node)`, so ties go to the lowest index. A test covers this, including permuting the input order. |
| Reuse | The flow output is the same `EscapeCluster` / `EscapeCorridorPlan`, grouped by `(exit face, layer, octant)`. Sizing (`_size_corridor`), the #4519 reserve gate and `reserve()` are all shared with greedy. No second reservation path was added. |
| Fallback | If the flow assigns no pin (`empty`) or leaves any pin unassigned (`infeasible`), the planner returns the unchanged greedy plan and records `fallback_reason`. An infeasible flow never commits copper: planning only reads cells, and reservations are SOFT attractors. |
| Selection | `Autorouter.escape_corridor_assignment = "greedy"` (the default) or `"flow"`. The CLI selector is `--escape-corridor-assignment {greedy,flow}`. It is on both route parsers, forwarded by the shim, and hidden from `--help`. `flow` implies `--escape-corridor-reservation`. |
| Instrumentation | Per package, the router logs `Escape-flow (REF): planned ...` or `fallback to greedy -- <reason>`. Per run it logs `Escape-flow summary: packages= planned= infeasible= empty= error=`. The harness marks a row `treatment_applied=False` when the summary is missing, when no package got a flow plan, or when no cells were reserved. An inert arm therefore cannot pass as a null result. |

Arms:

| Arm | `kct route` extra flags |
|---|---|
| A: default | none (pad-access invariant on, its default since #5891) |
| B: greedy corridors | `--escape-corridor-reservation` |
| C: flow corridors | `--escape-corridor-reservation --escape-corridor-assignment flow` |

Every arm also gets `--seed 42`. C against B isolates the joint-assignment
hypothesis. B against A re-baselines the corridor flag.

## Board set

The board set was reconfirmed immediately before measuring by running
`Autorouter.detect_dense_packages()` on every `boards/*/output/*.kicad_pcb`:

| Board | Dense packages | Role |
|---|---|---|
| 00, 01, 02 | none | control (01 is run): B and C must be inert |
| 03 | U1 QFP-44 | subject |
| 04 | U2 LQFP-48 (2 layers) | subject |
| 05 | U1 TQFP-32, U2 HTSSOP-28 with exposed-pad thermal vias (59 pads, classified BGA), U3 HVSSOP-8 with thermal vias (classified BGA) | subject; the #4474 motivating board (now rev B) |
| 06a | U1-U8 SOIC-8 | no-regression |
| 07 | U1 QFP-144, U2 TSOP-54 | not routed: 6 layers, too heavy for this shared host (see "Not measured") |
| 09 | J1 USB-C, Q1/Q2 SOP-8, U3 TSSOP-10, U1 | plan-level census only |

## Measurement conditions (read before trusting any number)

- Measured revision `5a211494` (this branch rebased on `origin/main`
  `02187dff`). Board 05 was measured at `609bc40d` / `b5ae7fd9`, and those
  commits change only the harness, the tests, the docs and the ecosystem TOML,
  so the router source is identical (`git diff 5a211494 b5ae7fd9 -- src/`
  touches only `ecosystem/data/projects.toml`). C++ backend `available (version 1.0.0)`, build version 55.
  Linux 6.17 on AWS, **8 logical CPUs, shared** with other sweeps. The load
  average at the start of a row ranged from 1.8 to 5.9 (per-row
  `loadavg_start` is in the JSON).
- One run per (board, arm), plus a second run of board 05's B and C, all
  sequential with no parallel fan-out. On 01, 03, 04 and 06a the copper
  metrics are stable given the seed. Board 05's arm C is not: see the copper
  fingerprints. **Seconds are never stable**: trust the copper columns and
  A\* call counts, not wall time.
- Grading uses `measure_completion` / `measure_copper` from
  `kicad_tools.benchmark.external.metrics`, the same referee path as
  `ripup_experiment.py`. Access classes come from each run's
  `*.access_witness.json` sidecar. "Copper identical" means the sorted
  `segment`/`via`/`arc` s-expressions, with `uuid` stripped, hash equal.
- No `kicad-cli` DRC referee. Every arm is kct against kct on the same input,
  and C changes only a SOFT attractor. Where C's copper is byte-identical to B's,
  its DRC is identical by construction.
- Inputs are read from `boards/`, and `route_phase_profile.py` routes a copy
  in the work dir. `git status` confirms that no tracked board artifact
  changed.

## Results

`exit` is `kct route`'s exit code. Corridor cells are the SOFT cells reserved
(this line is absent when no cluster passes the #4519 gate). "Flow planned" is
the number of packages that received a flow plan. Infeasible and empty
fallbacks were 0 on every row. Stranded counts terminals whose final access
set is empty.

| Board | Arm | Applied | exit | Nets | Connections | Vias | Wirelength mm | Segments | Corridor cells | Flow planned | Stranded | A\* calls | Routing-loop s | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | A | yes | 0 | 3/3 | 5/5 | 5 | 65.25 | 31 | 0 | -- | 0 | 72 | 3.43 | 4.6 |
| 01 | B | **inert** | 0 | 3/3 | 5/5 | 5 | 65.25 | 31 | 0 | -- | 0 | 72 | 3.56 | 4.8 |
| 01 | C | **inert** | 0 | 3/3 | 5/5 | 5 | 65.25 | 31 | 0 | 0 | 0 | 72 | 3.41 | 4.6 |
| 03 | A | yes | 0 | 27/27 | 115/115 | 75 | 718.87 | 388 | 0 | -- | 0 | 265 | 101.1 | 198.5 |
| 03 | B | yes | 0 | 27/27 | 115/115 | 75 | 718.87 | 388 | 578 | -- | 0 | 265 | 121.1 | 218.4 |
| 03 | C | yes | 0 | 27/27 | 115/115 | 75 | 718.87 | 388 | 429 | 1/1 | 0 | 265 | 120.3 | 221.6 |
| 04 | A | yes | 0 | 12/12 | 43/43 | 35 | 212.53 | 112 | 0 | -- | 0 | 38 | 6.12 | 19.7 |
| 04 | B | yes | 0 | 12/12 | 43/43 | 35 | 212.58 | 114 | 617 | -- | 0 | 38 | 7.44 | 22.0 |
| 04 | C | yes | 0 | 12/12 | 43/43 | 35 | 212.58 | 114 | 617 | 1/1 | 0 | 38 | 7.56 | 22.4 |
| 06a | A | yes | 0 | 18/18 | 90/90 | 55 | 489.40 | 183 | 0 | -- | 0 | 56 | 6.81 | 15.4 |
| 06a | B | yes | 0 | 18/18 | 90/90 | 55 | 489.40 | 184 | 1982 | -- | 0 | 56 | 7.51 | 16.1 |
| 06a | C | yes | 0 | 18/18 | 90/90 | 55 | 489.40 | 184 | 1982 | 8/8 | 0 | 56 | 7.36 | 15.7 |
| 05 | A (1800 s cap) | -- | -- | TIMEOUT | -- | -- | -- | -- | -- | -- | -- | -- | -- | 1800.2 |
| 05 | A | yes | **2** | 28/37 | 131/142 | 107 | 1037.68 | 2414 | 0 | -- | 0 | 1466 | 874.7 | 1823.5 |
| 05 | B | yes | **4** | 27/37 | 132/142 | **127** | **1110.00** | 1001 | 5192 ×3 | -- | 0 | 1408 | 1030.9 | 1711.2 |
| 05 | B (re-run) | yes | 4 | 27/37 | 132/142 | 127 | 1110.00 | 1001 | 5192 ×3 | -- | 0 | 1407 | -- | 1710.7 |
| 05 | C | yes | 2 | 28/37 | 131/142 | 106 | 1039.93 | 2182 | 4419, 4419, 2697 | 9/9 | 0 | 1400 | 857.9 | 1692.0 |
| 05 | C (re-run) | yes | 2 | 28/37 | 131/142 | 108 | 1035.14 | 2350 | 4419, 4419, 2697 | 9/9 | 0 | 1563 | -- | 2032.2 |

Board 05 routes three times: the layer-escalation ladder is \[4L, 4L, 6L\],
and each pass re-plans the corridors, so its corridor cells are listed per
pass. The first matrix capped each run at 1800 s, and board 05's arm-A row
hit that cap during post-route work. That TIMEOUT row is kept above. All
three arms of board 05 were then re-run with a 3600 s cap, and arms B and C
were run a second time to check reproducibility.

Copper fingerprints (sorted copper items / hash prefix):

| Board | A | B | C |
|---|---|---|---|
| 01 | 36 / `48937e86` | 36 / `48937e86` | 36 / `48937e86` |
| 03 | 463 / `c9189f86` | 463 / `c9189f86` | 463 / `c9189f86` |
| 04 | 147 / `fcf75b86` | 149 / `ff077b22` | 149 / `ff077b22` |
| 06a | 238 / `9da64322` | 239 / `7b6333bd` | 239 / `7b6333bd` |
| 05 | 2521 / `fb89934c` | 1128 / `1f483bdd` (re-run identical) | 2288 / `e99474e1`; re-run 2458 / `5bd9f344` |

Board 05's arm C is **not** run-to-run deterministic, even though B is.
Both C runs land in the same outcome class (exit 2, 131/142 connections, 28/37
nets, 106-108 vias, 1035-1040 mm), but their copper differs. kct route on
board 05 has wall-clock budgets: a 60 s escape budget, the placement-delta
loop and per-net caps. On a loaded host those can resolve differently from
run to run. Only one A run completed, so A's own variance is unknown. Read
A against C on board 05 as "the same class", not as a ranking of 106 against
107 vias.

### C fired, and still moved no copper

The treatment was not a silent no-op:

- **Board 03.** Greedy reserved 9 clusters and 578 cells. Flow reserved 14
  clusters and 429 cells over the same layers \[1, 2\], so the reserved cell
  sets differ. The routed copper is byte-identical in A, B and C.
- **Board 04.** Both strategies reserved 6 clusters and 617 cells. Greedy
  round-robined them over layers \[0, 1\]. Flow put all of them on layer 0:
  on a 2-layer board, an SMD pad's own layer has no via cost, and 8 pins never
  build enough convex load to pay for a via. The copper is still identical
  between B and C.
- **Board 06a.** Inside `kct route`, each SOIC-8 has exactly one signal pin
  left after pour nets and pre-routed LVDS are excluded. A one-pin flow is
  greedy, and the plans are identical.
- **Board 05.** In the first pass, greedy reserved U1 10 / U2 6 / U3 4
  clusters (1014 / 3443 / 735 cells). Flow reserved 12 / 12 / 4 clusters
  (1095 / 2589 / 735 cells). The flow moved 3 of U2's 22 signal pins to an
  adjacent face, a cost choice, since U2's exits had capacity 160, and put all
  of U3 on layer 1. In the 6-layer pass the flow spread U1 and U2 over layers
  1-4 with 777 and 1185 cells, where greedy kept 1014 and 3443. So the flow
  reserved fewer cells in total (11 535 against 15 576 over three passes).
  Corridors in that smaller footprint did not trigger B's clearance-violation
  outcome. Whether the improvement comes from *less* reservation or from
  *better-placed* reservation was not separated (see "Not measured").

### Plan-level census: capacity never binds

`--plans-only` runs both strategies on fresh routers loaded with
`load_pcb_for_routing`, without routing. It counts *every* netted pin,
including the pour and pre-routed nets that `kct route` excludes, so its pin
counts are higher than in the routed rows. That makes it a harder test of
capacity than the routed runs:

| Board | Part | Pins | Capacity | Greedy clusters/cells/layers | Flow clusters/cells/layers | Pins moved off nearest face | Saturated exits | Max exit utilisation | Cell overlap (Jaccard) |
|---|---|---|---|---|---|---|---|---|---|
| 03 | U1 | 29 | 224 | 16/460/[1, 2] | 22/1530/[1, 2] | 0 | 0 | 0.18 | 0.03 |
| 04 | U2 | 17 | 160 | 14/626/[0, 1] | 14/626/[0] | 0 | 0 | 0.3 | 0.36 |
| 05 | U1 | 26 | 160 | 13/923/[1, 2] | 16/722/[1, 2] | 0 | 0 | 0.2 | 0.37 |
| 05 | U2 | 53 | 140 | 11/4160/[1, 2] | 18/3148/[1, 2] | 7 | 0 | 0.43 | 0.21 |
| 05 | U3 | 12 | 48 | 5/316/[1, 2] | 7/877/[1, 2] | 4 | 0 | 0.5 | 0.09 |
| 06a | U1 | 5 | 84 | 5/787/[1, 2] | 5/787/[1, 2] | 1 | 0 | 0.11 | 0.66 |
| 06a | U2 | 5 | 84 | 5/306/[1, 2] | 5/306/[1, 2] | 2 | 0 | 0.22 | 0.33 |
| 06a | U3 | 5 | 84 | 4/544/[1, 2] | 5/618/[1, 2] | 2 | 0 | 0.11 | 0.47 |
| 06a | U4 | 5 | 84 | 4/255/[1, 2] | 5/459/[1, 2] | 2 | 0 | 0.11 | 0.08 |
| 06a | U5 | 5 | 84 | 5/789/[1, 2] | 5/789/[1, 2] | 1 | 0 | 0.11 | 0.66 |
| 06a | U6 | 5 | 84 | 5/783/[1, 2] | 5/783/[1, 2] | 2 | 0 | 0.22 | 0.67 |
| 06a | U7 | 5 | 84 | 5/639/[1, 2] | 5/639/[1, 2] | 2 | 0 | 0.11 | 0.31 |
| 06a | U8 | 5 | 84 | 5/306/[1, 2] | 5/306/[1, 2] | 2 | 0 | 0.11 | 1.0 |
| 09 | J1 | 14 | 132 | 5/988/[1, 2] | 8/1108/[1, 2] | 0 | 0 | 0.24 | 0.24 |
| 09 | Q1 | 8 | 84 | 6/1027/[1, 2] | 6/1021/[1, 2] | 2 | 0 | 0.22 | 0.0 |
| 09 | U3 | 10 | 60 | 8/1045/[1, 2] | 9/1045/[1, 2] | 2 | 0 | 0.4 | 0.31 |
| 09 | U1 | 21 | 72 | 11/514/[1, 2] | 18/1201/[1, 2] | 0 | 0 | 0.44 | 0.0 |
| 09 | Q2 | 8 | 84 | 7/831/[1, 2] | 7/830/[1, 2] | 2 | 0 | 0.22 | 0.46 |

Across 18 dense parts on five boards, no exit saturates and no exit has zero
capacity. Total capacity is at least 2.6× the pin count (05 U2: 140 for 53).
The pins that do move off their nearest face (05 U2/U3, the SOP-8s) move for
*cost* reasons: their destination lies past a corner, so the cheapest exit is
the adjacent face. They do not move because a channel is full. A greedy
"cheapest exit per pin" would make the same choice. **On this fleet the flow
is a per-pin argmin.**

The low Jaccard overlaps are real geometry changes. The flow groups by
`(exit face, layer, octant)` and launches corridors from the exit point, not
from the pin centroid, and the #4519 gate then selects a different subset. But
as the routed rows show, a different SOFT corridor does not produce different
copper.

## Interpretation

- **C against B (the hypothesis):** no effect on 01, 03, 04 and 06a, where
  copper was identical. On 05, C is better than B: no clearance violations,
  about 20 fewer vias and 70-75 mm less wire, for one connection fewer and one complete net more.
- **C against A (the decision that matters):** the same outcome class on
  every board. C never routed more connections or nets than A. Adopting C
  means opting into corridor reservation, and corridor reservation has not
  earned its place: B against A is inert on four boards and a regression on
  the fifth.
- **B against A (the corridor flag itself):** this re-baselines the #4519
  history. The flag stays a net negative on board 05 under the pad-access
  invariant. That agrees with the #5891 docstring, which calls it superseded.
- **Why joint allocation cannot help here:** the fleet's dense parts are not
  escape-capacity-limited under this capacity mapping, and the failures on
  board 05 are search failures, not boxed-in pads. A joint solver has nothing
  to resolve.

## Not measured

- **A capacity-bound package.** The joint allocation's real value, resolving
  oversubscribed exits, was never tested on a real board, because no exit
  saturated. That needs a fine-pitch BGA with inner rows, or a capacity mapping
  stricter than one track per pitch of free probe line (for example
  counting only cells reachable from the pins, or subtracting cells used by
  vias). The unit tests cover oversubscription on hand-built graphs only.
- **A stronger reservation.** The treatment is limited to the existing SOFT
  reservation (the issue rules out hard fences). A flow plan that drives
  something stronger, such as the escape router's layer choice, per-pin escape
  direction, or a HARD corridor, might show an effect. That would be a
  different experiment.
- **Less reservation against better reservation on board 05.** C reserved
  fewer cells than B. A "greedy with C's cell budget" arm would separate the
  two explanations. Since C only matches A, the question does not change the
  verdict.
- **Variance of arm A on board 05.** Only one uncapped A run completed (see
  the copper fingerprints).
- **Board 07** (6 layers, QFP-144 and TSOP-54). It is the most
  capacity-plausible subject in the repo, but it is too heavy to route three
  times on this shared 8-CPU host. It is the first board to add on a quiet
  host.
- **Board 09 routing.** Board 09 is in the plan-level census only.
- **Cost-model sensitivity.** The values used are `under_body_factor` 2.0, via
  1.0 mm, congestion 2.0 mm and load step 0.05 mm, and they were not swept.
  With capacity slack everywhere, the factor that could matter is the via cost
  against the load step on 2-layer boards.
- **DRC outcomes.** None were needed where copper is identical (see
  conditions). On board 05 the only DRC signal is kct's own exit code: 4
  means segment-segment or pairwise clearance violations remained, and 2
  means none did.
- **A quiet-host timing re-run.** The timing columns are contention-dominated.

## Follow-up

Adoption is declined, so no production issue is filed. If a capacity-bound
board joins the fleet, re-run this harness on it first. A refine experiment
(stricter capacity mapping, plus driving the escape router's layer choice from
the flow) would be a new child of #5947.

## Reproduce

```bash
uv run kct build-native --check   # must report "available"; see CLAUDE.md
# Routed three-arm matrix (sequential; board 05 needs a larger cap):
uv run python scripts/research/escape_flow_experiment.py \
    --boards 01,04,06a,03 --arms A,B,C \
    --work-dir /tmp/escape-flow --json /tmp/escape-flow/summary.json --timeout 1800
uv run python scripts/research/escape_flow_experiment.py \
    --boards 05 --arms A,B,C \
    --work-dir /tmp/escape-flow-05 --json /tmp/escape-flow-05/summary.json --timeout 3600
# Plan-level census (no routing, seconds):
uv run python scripts/research/escape_flow_experiment.py \
    --plans-only --boards 03,04,05,06a,09
# Unit tests:
uv run pytest tests/test_escape_flow.py tests/router/test_escape_corridor.py --no-cov
```
