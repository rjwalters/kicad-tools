# Differential Pair Test Board (Board 06)

Regression testbench for the differential-pair routing subsystem introduced
by Epic [#2556](https://github.com/rjwalters/kicad-tools/issues/2556)
(Phases 1-3).  Demonstrates each protocol family the epic was scoped against
(USB 2.0, USB 3.0, PCIe Gen1, MIPI D-PHY) by routing one or more diff pairs
per protocol on a 4-layer JLCPCB tier-1 stackup.

**This is not a working device.**  The source connectors (USB-C, mini-PCIe
edge, FFC) drive synthetic sink footprints (QFN, QFP, BGA simulator).  The
board's purpose is to exercise every Phase 1-3 net-class feature on at least
one pair so future router/validate changes have an end-to-end witness on
disk.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/06-diffpair-test

# Or run specific steps
kct build boards/06-diffpair-test --step schematic
kct build boards/06-diffpair-test --step pcb
kct build boards/06-diffpair-test --step route
kct build boards/06-diffpair-test --step verify

# Run DRC against the committed routed PCB
kct check boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb --mfr jlcpcb
```

## Stackup

4-layer JLCPCB tier-1:

| Layer index | KiCad name | Purpose                              |
|-------------|-----------|--------------------------------------|
| 0           | F.Cu      | Signal (escape + outer routing)      |
| 1           | In1.Cu    | GND plane (impedance reference)      |
| 2           | In2.Cu    | PWR plane (+3V3 / +1V8 / +1V2)       |
| 31          | B.Cu      | Signal (optional bottom escape)      |

This is what the Phase 3K impedance formulas
(`src/kicad_tools/router/diffpair_impedance.py`) were calibrated against.

## Protocol Scenarios

| Scenario   | Pairs | Speed         | Source -> Sink                       | Phase features exercised                                          |
|------------|-------|---------------|--------------------------------------|-------------------------------------------------------------------|
| USB 2.0    | 1     | 480 Mbps      | USB-C (J1) -> QFN-32 (U1)            | 1C clearance, 2E coupled, 2G continuity, 3K 90 Ohm diff           |
| USB 3.0    | 4     | 5 Gbps        | USB-C (J1) -> BGA-49 simulator (U2)  | 1C, 2E, 2F BGA escape, 2G tight (0.9), 3K 90 Ohm, 3H 0.5 mm skew  |
| PCIe Gen1  | 2     | 2.5 Gbps      | Mini-PCIe edge (J3) -> QFP-48 (U3)   | 1C, 2E, 3H 0.5 mm skew, 3I serpentine, 3J skew DRC, 3K 100 Ohm    |
| MIPI D-PHY | 2     | 1 Gbps/lane   | FFC (J4) -> QFN-24 (U4)              | 1C, 2E, 3I serpentine (tight 0.3 mm), 3K 100 Ohm                  |

**9 differential pairs / 18 paired nets**.  Combined with ground / power /
single-ended sideband (USB_CC1, USB_CC2, MIPI_RST), the board has **26 nets
total** --- comparable in scale to board 03 (13 nets) and well below
board 05 (~50 nets).

## Components

| Reference | Description                              | Footprint                                          |
|-----------|------------------------------------------|---------------------------------------------------|
| J1        | USB-C receptacle (USB 2.0 + USB 3.0)     | `Connector_USB:USB_C_Receptacle_USB2.0`           |
| J3        | Mini-PCIe card-edge (synthetic)          | `Connector_PCIE:PCIE_Mini_Edge`                   |
| J4        | 4-pin FFC, 0.5 mm pitch                  | `Connector_FFC:FFC_4P_0.5mm`                      |
| U1        | QFN-32, 0.5 mm pitch (USB 2.0 sink)      | `Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm`         |
| U2        | BGA-49 simulator, 0.5 mm pitch           | `Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm`   |
| U3        | QFP-48, 0.5 mm pitch (PCIe sink)         | `Package_QFP:LQFP-48_7x7mm_P0.5mm`                |
| U4        | QFN-24, 0.5 mm pitch (MIPI sink)         | `Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm`         |

All sinks are placed on F.Cu so all routing happens on the outer signal
layers; GND/PWR planes are unencumbered for impedance reference.

## Per-Protocol Net Class Declarations

The protocol-specific `NetClassRouting` instances live in `generate_design.py`:

- `usb2_net_class()` --- `target_diff_impedance=90`, `intra_pair_clearance=0.075`
- `usb3_net_class()` --- `target_diff_impedance=90`, `coupled_continuity_threshold=0.9`
- `pcie_net_class()` --- `target_diff_impedance=100`, `skew_tolerance_mm=0.5`
- `mipi_net_class()` --- `target_diff_impedance=100`, `skew_tolerance_mm=0.3`
- `sideband_net_class()` --- `target_single_impedance=50` (for USB_CC1/CC2, MIPI_RST)

`build_net_class_map()` assembles them into a `net_name -> NetClassRouting`
dict that both the autorouter (in `route_pcb()`) and the regression test
(`tests/test_board_06_diffpair_test.py::test_phase_features_exercised`)
import.  This ensures test-implementation parity.

## Files

| File                                          | Description                                          |
|-----------------------------------------------|------------------------------------------------------|
| `project.kct`                                 | KCT v1.0 spec (manufacturing/intent metadata)        |
| `generate_schematic.py`                       | Emits the schematic (`output/diffpair_test.kicad_sch`) |
| `generate_pcb.py`                             | Emits the unrouted PCB + holds the NETS / DIFFPAIRS dicts |
| `generate_design.py`                          | End-to-end pipeline (schematic + PCB + route + DRC)  |
| `output/diffpair_test.kicad_sch`              | Generated schematic (committed)                      |
| `output/diffpair_test.kicad_pcb`              | Unrouted PCB (committed)                             |
| `output/diffpair_test_routed.kicad_pcb`       | Routed PCB (committed --- consumed by CI DRC gate)   |

## DRC Status

The routed PCB is checked against JLCPCB tier-1 rules via:

```bash
kct check output/diffpair_test_routed.kicad_pcb --mfr jlcpcb --errors-only
```

**Phase-3 dependencies**:

- [#2649](https://github.com/rjwalters/kicad-tools/issues/2649)
  (Phase 3J `diffpair_length_skew` DRC rule) --- **landed** in PR #2662.
  Once the router populates `skew_data`, the rule fires on PCIe / MIPI
  skew violations.
- [#2648](https://github.com/rjwalters/kicad-tools/issues/2648)
  (Phase 3I serpentine insertion) --- **landed** (closed).  At
  scaffold-time it was still in progress, so PCIe / MIPI pairs could
  exceed their declared `skew_tolerance_mm` because the router couldn't
  yet meander to fix skew.

*Historical scaffold-time context*: per the Epic #2556 Phase 4L
mitigation strategy, this board was scaffolded with PCIe / MIPI pairs
declared but DRC tolerated at "non-strict" while #2648 was pending.
Both dependencies have since landed; the remaining tolerated residual is
the 18-error diff-pair quality block documented under "CI Gate" below.

## Measuring Changes: the Shadow Phase Is Deterministic, Full Regen Is Not (#4536)

Full board-06 regeneration (`generate_design.py --step route`) is
run-to-run nondeterministic downstream of the coupled diff-pair
pre-phase --- two same-seed runs of unmodified `main` can differ by
thousands of lines ([#4536], open).  Every agent that measures a board-06
change by diffing two full regens rediscovers this the hard way; this
section exists so that discovery is a paragraph instead of a multi-hour
detour.

**The shadow-construction phase itself is fully deterministic.** This was
established independently more than once during a 5-issue diffpair sweep
(2026-08-03/04, [#4570]/[#4574]/[#4575]/[#4577]/[#4579]/[#4581]/[#4582]):
two full runs at the same commit produce byte-identical sets of all 33
`[coupled-tail]` lines, all 47 `[coupled-follow] declined` lines, and 323
gate rejections from the #4575 via-clearance gate (split 3+15+68+107+82+7+34+7
across the 9 pairs). The #4582 Judge independently reproduced the same
323 figure across four separate runs spanning both downstream modes
below. The nondeterminism is entirely downstream of this phase.

Reproduce the shadow phase and its debug trace:

```bash
KCT_BOARD06_SHADOW=1 PYTHONHASHSEED=42 uv run python \
  boards/06-diffpair-test/generate_design.py --step route --seed 42
# KCT_SHADOW_DEBUG=1 adds the [coupled-tail] / [coupled-follow] instrumentation
```

### The two downstream modes

**All figures in this subsection are from the shadow-ON configuration
(`KCT_BOARD06_SHADOW=1`, the repro block above).  The default recipe
runs shadow-OFF** --- `ENABLE_COUPLED_SHADOW` reads `KCT_BOARD06_SHADOW`
with default `"0"` (`generate_design.py`, the #3508 block), which is what
the committed artifact and CI use.

Once shadow output feeds the negotiated `route_all_negotiated` pass, the
shadow-ON board settles into one of two stable modes rather than a
continuum:

| (shadow-ON) | DRC errors | `diffpair_clearance_intra` | reach |
|---|---|---|---|
| Mode A | 32 | 7 | 19 of 21 |
| Mode B | 35 | 19 | 18 of 21 |

`coupled-ok` is 5/9 in both modes --- the shadow-ON convergence figure
recorded in the recipe comments ("Convergence is 5/9 (post-#4576)").  A
run's mode is identifiable from three mutually consistent signals ---
total DRC error count, the `diffpair_clearance_intra` count, and reach
--- which is what makes paired mode-for-mode comparison possible instead
of guesswork.

**If you ran with the default (shadow-OFF), you are not in either
mode.** Expect the committed / CI floor instead: **18 DRC errors**
(9 `diffpair_length_skew` + 9 `diffpair_routing_continuity`, the value
pinned for this board in `.github/routed-drc-tolerance.yml`) at **21 of
21** reach --- the shadow-OFF reach recorded in the same recipe
comments.  A default-config run landing on those numbers is the expected
result, not an unrecognized third mode.

### Measurement convention

- **Compare shadow-phase counters directly.** The `[coupled-tail]` /
  `[coupled-follow] declined` line counts and the per-pair gate-rejection
  split are deterministic; a difference there is a real signal.
- **Compare full-regen (board-level) results paired mode-for-mode.**
  Identify each side's mode from the three signals above before
  comparing error counts, `diffpair_clearance_intra`, or reach across a
  change.
- **Never assert a delta from a single run of each side.** A lone
  before/after pair can silently compare Mode A against Mode B and
  report a mode flip as a regression or a win. Run enough repeats to
  identify the mode on both sides first.

Two traps this has already cost people:

1. A Builder's first baseline landed in Mode A while the Curator's had
   landed in Mode B, so the two disagreed on the "current" numbers and
   neither was wrong.
2. A Builder ran a narrow-vs-broad comparison that looked decisive and
   was pure mode noise --- only paired sampling separated signal from
   [#4536].

### Standing invariants and the vacuity trap

A change should not drop `coupled-ok` below 5/9, and should not drop
reach when compared mode-for-mode.  **The 5/9 figure is the shadow-ON
convergence number** recorded in the recipe comments next to
`ENABLE_COUPLED_SHADOW`; like the mode table, it is an invariant for
shadow-ON runs, so evaluate it against a shadow-ON baseline rather than
a default (shadow-OFF) one.  Watch for the **vacuity trap**:
`diffpair_length_skew` only fires on an *engaged* (coupled) pair, so a
change that knocks a pair from coupled to declined removes it from
skew-checking entirely --- the error count can drop while the change is
a regression, not a win.  Always read `coupled-ok` and reach alongside
the raw error count, never the error count alone.

### The crossover legality census (`KCT_CROSSTAIL_CENSUS=1`)

The shadow constructor's crossover tails ship the **first** legal via-site
candidate out of a 225-entry lattice and report nothing about the rest, so an
*ordering* problem (many sites legal, a better key would pick a kinder one)
looks exactly like a *saturation* one (almost nothing is legal, so no key can
help).  `KCT_CROSSTAIL_CENSUS=1` ([#4580]) scans the whole lattice instead and
prints one header per crossover:

```bash
KCT_CROSSTAIL_CENSUS=1 KCT_BOARD06_SHADOW=1 PYTHONHASHSEED=42 uv run python \
  boards/06-diffpair-test/generate_design.py --step route --seed 42
# [crosstail-census] net=… head=(…) goal=(…) legal=2/225 distinct_v1=1 census_s=0.0164
```

`distinct_v1` is the field that separates the two worlds: a legal set that is a
singleton in `v1` carries the same barrel on every legal route, so no ordering
key can move the result and the constraint lives upstream in placement / escape
planning.

**State-neutral is not the same as budget-neutral ([#4635]).** PR [#4611]'s
prose claimed the census "cannot" change the route because it is
"observation-only by construction".  That is true of router **state** — every
gate the scan calls past the accept point is mutation-free, so continuing the
sweep cannot perturb anything.  It was **not** true of the wall-clock
**budget**: the census runs inside the shadow phase's per-pair window, and every
downstream deadline there is computed as `<budget> - <elapsed since the window
opened>`, so census seconds were silently deducted from the probes that follow.
A census-on run could therefore differ from a census-off run through budget
pressure alone.  Since #4635 the census credits its own **incremental** cost
(the sweep after the first legal candidate — zero when nothing is legal, since
both modes then scan the whole lattice) back to that window, so census-on and
census-off runs get the same effective downstream budget.  The trade is
deliberate: a census-on pair's *true* wall clock may now exceed the 30 s
`_SHADOW_PER_PAIR_BUDGET_S` by the census's own cost, which stays visible in
`census_s=` and in the unadjusted per-pair `[coupled-timing] elapsed=` line.

**Measured on this board** (shadow-ON, seed 42, 2026-08-04 — re-measure before
quoting): 166 crossovers, of which **150 (90.4%) have `legal=0/225`** and so
credit exactly zero.  Total credited census time across the whole shadow phase
is **1.28 s**; the worst single pair (USB3_TX1) accrues **0.86 s**, i.e. under
3% of its 30 s budget.  That is a **refutation** of the budget-pressure
hypothesis for the Mode A / Mode B flip: board-06's lattice is saturated, the
un-instrumented loop already scans most of the 225 candidates before finding
anything, and the incremental cost is therefore small.  [#4536] bimodality
remains the leading explanation for a 35-vs-32 difference.  (On an *open*
lattice the picture reverses — a synthetic fixture whose first legal candidate
is at rank 0 pays ~3.9 ms of a ~4.0 ms census, ~39x the un-instrumented
0.10 ms.)

[#4536]: https://github.com/rjwalters/kicad-tools/issues/4536
[#4570]: https://github.com/rjwalters/kicad-tools/issues/4570
[#4574]: https://github.com/rjwalters/kicad-tools/issues/4574
[#4575]: https://github.com/rjwalters/kicad-tools/issues/4575
[#4577]: https://github.com/rjwalters/kicad-tools/issues/4577
[#4579]: https://github.com/rjwalters/kicad-tools/issues/4579
[#4580]: https://github.com/rjwalters/kicad-tools/issues/4580
[#4581]: https://github.com/rjwalters/kicad-tools/issues/4581
[#4582]: https://github.com/rjwalters/kicad-tools/issues/4582
[#4611]: https://github.com/rjwalters/kicad-tools/pull/4611
[#4635]: https://github.com/rjwalters/kicad-tools/issues/4635

## CI Gate (Phase 4N, #2660)

This board is **re-routed from scratch on every pull request** by the
`diffpair-routing-regression` job in `.github/workflows/ci.yml`.  Unlike
the diff-driven `routed-pcb-drc-check` job (which only runs when a PR
touches a committed `*_routed.kicad_pcb`), this job always runs so that
algorithmic regressions in the router are caught even when the committed
PCB stays untouched.

The job:

1. Runs `python boards/06-diffpair-test/generate_design.py --step route --seed 42`
   to re-route the unrouted PCB deterministically.
2. Loads the freshly-routed PCB, constructs the per-protocol
   `NetClassRouting` map from `build_net_class_map()`, and runs
   `DRCChecker` with `--mfr jlcpcb`.
3. Asserts the DRC **error count** is within the per-board allowlist in
   `.github/routed-drc-tolerance.yml` (currently 18: 9x
   `diffpair_length_skew` + 9x `diffpair_routing_continuity` per
   [#3540](https://github.com/rjwalters/kicad-tools/issues/3540)-[#3544](https://github.com/rjwalters/kicad-tools/issues/3544),
   floor ratcheted 24 -> 18 by
   [#4019](https://github.com/rjwalters/kicad-tools/issues/4019) with all
   three gated paths --- committed-artifact `routed-pcb-drc-check`,
   seed-42 Diff-Pair Routing Regression, and the Board 06 E2E fresh
   regen --- agreeing at 18).
4. Asserts each of the three diff-pair DRC rule_ids was actually
   exercised by the check, i.e. the JSON summary's
   `rules_checked_by_rule[rule_id] >= 1` for each of:
     - `diffpair_clearance_intra`
     - `diffpair_length_skew`
     - `diffpair_routing_continuity`

   This guards against silent regressions that disable diff-pair
   detection (e.g. flipping `coupled_routing` back to `False` on the
   net classes).  Without this assertion a regression that produces 0
   diff-pair violations because no rule ran would slip through the
   allowlist check.

### Interpreting a failure

| Failure mode                                                | Likely cause                                                      |
|-------------------------------------------------------------|-------------------------------------------------------------------|
| `Diff-pair rule(s) NOT exercised`                           | Detection broken: `coupled_routing` flag flipped, suffix detection broken, or the routed PCB has no matching pair traces. |
| `DRC regression: <N> error(s) exceeds allowlist value 18`   | The router introduced new DRC errors beyond the documented #3540-#3544 baseline (9x skew + 9x continuity, #4019 ratchet).  Bisect against the routing algorithm. |
| Re-route step fails with non-zero exit                      | Routing algorithm regression (board hangs or crashes during `route_all`).  Reproduce locally with `--seed 42`. |

### Tightening the allowlist

The original prerequisites ---
[#2672](https://github.com/rjwalters/kicad-tools/issues/2672)
(impedance-width selection) and
[#2677](https://github.com/rjwalters/kicad-tools/issues/2677) (BGA
partner-via escape) --- are both **closed**, and their error classes are
gone from the gate.  The floor was ratcheted 24 -> 18 by
[#4019](https://github.com/rjwalters/kicad-tools/issues/4019); the
current residual is exactly the diff-pair quality block (9x
`diffpair_length_skew` + 9x `diffpair_routing_continuity`,
[#3540](https://github.com/rjwalters/kicad-tools/issues/3540)-[#3544](https://github.com/rjwalters/kicad-tools/issues/3544)).
That residual must clear before the board 06 entry in
`.github/routed-drc-tolerance.yml` can shrink further.  Eventually the
entry should be **removed** entirely (per the YAML's "absence == strict
0 errors" convention) once the board routes cleanly under JLCPCB tier-1
rules.

## Out of Scope

Per issue [#2658](https://github.com/rjwalters/kicad-tools/issues/2658)
Scope (out):

- **No new router / validate features**.  This board EXERCISES Phases 1-3;
  it doesn't add new router logic.  Missing-feature gaps file separate
  issues under #2556.
- **No reference circuits**.  No actual USB 3.0 PHY, no PCIe root complex.
  Connector -> breakout footprint is enough.
- **No 5th protocol** (DDR, HDMI, etc.).  Scope is fixed to USB 2/USB 3/
  PCIe/MIPI.

## Related Issues

- Epic: [#2556](https://github.com/rjwalters/kicad-tools/issues/2556) (first-class diff-pair support)
- This issue: [#2658](https://github.com/rjwalters/kicad-tools/issues/2658) (Phase 4L scaffolding)
- CI integration: Phase 4N (consumes the routed PCB committed here)
- Documentation: Phase 4M (cites this board's `project.kct` + net-class declarations as canonical examples)
