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
  (Phase 3I serpentine insertion) --- still in progress at scaffold-time.
  Until it lands, PCIe / MIPI pairs may exceed their declared
  `skew_tolerance_mm` because the router can't yet meander to fix skew.

Per the Epic #2556 Phase 4L mitigation strategy, this board is scaffolded
now with PCIe / MIPI pairs declared but DRC tolerated at "non-strict" if
#2648 hasn't landed yet.  Re-route and tighten when #2648 merges.

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
   `.github/routed-drc-tolerance.yml` (currently 28: 25x `ImpedanceRule`
   per [#2672](https://github.com/rjwalters/kicad-tools/issues/2672)
   + 3x `diffpair_clearance_intra` per
   [#2677](https://github.com/rjwalters/kicad-tools/issues/2677)).
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
| `DRC regression: <N> error(s) exceeds allowlist value 28`   | The router introduced new DRC errors beyond the documented #2672/#2677 baseline.  Bisect against the routing algorithm. |
| Re-route step fails with non-zero exit                      | Routing algorithm regression (board hangs or crashes during `route_all`).  Reproduce locally with `--seed 42`. |

### Tightening the allowlist

When [#2672](https://github.com/rjwalters/kicad-tools/issues/2672)
(impedance-width selection) and
[#2677](https://github.com/rjwalters/kicad-tools/issues/2677) (BGA
partner-via escape) land, the board 06 entry in
`.github/routed-drc-tolerance.yml` should be reduced.  Eventually the
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
