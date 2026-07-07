# kicad-tools Demo Boards

Complete PCB designs demonstrating kicad-tools capabilities. Each board includes a `.kct` project specification file describing the design intent, requirements, and progress.

## Quick Start

The easiest way to build any demo is using the `kct build` command:

```bash
# Build any project (runs full pipeline: schematic → PCB → route → verify)
kct build boards/01-voltage-divider

# Or specify a .kct file directly
kct build boards/02-charlieplex-led/project.kct

# Run individual steps
kct build boards/01-voltage-divider --step schematic
kct build boards/01-voltage-divider --step pcb
kct build boards/01-voltage-divider --step route
kct build boards/01-voltage-divider --step verify

# Preview what would happen without running
kct build boards/01-voltage-divider --dry-run

# Target a specific manufacturer for DRC verification
kct build boards/01-voltage-divider --mfr jlcpcb
```

## Board Status

| # | Board | Status | Components | Nets | Notes |
|---|-------|--------|------------|------|-------|
| 01 | [Voltage Divider](01-voltage-divider/) | ✅ Working | 4 | 3 | Simplest possible design, workflow validation |
| 02 | [Charlieplex LED](02-charlieplex-led/) | ✅ Working | 14 | 8 | Routes 8/8 signal nets; both DRC engines clean (verified 2026-07-05) |
| 03 | [USB Joystick](03-usb-joystick/) | ✅ Working | ~20 | 13 | Routes 13/13 on 2-layer in ~24s, 0 native DRC; recipe requests `--differential-pairs` (runtime coupling gated on [#3952]) |
| 04 | [STM32 Dev Board](04-stm32-devboard/) | ✅ Working | ~30 | 12 | Fully routed via `generate_design.py`; kicad-cli DRC clean (verified 2026-07-05) |
| 06 | [Diff-Pair Test](06-diffpair-test/) | ⚠️ Scaffold | 7 | 26 | Epic [#2556] Phase 4L regression bench --- USB 2.0/3.0, PCIe, MIPI on 4-layer; exercises Phase 1-3 features (intra_pair_clearance, coupled_routing, coupled_continuity_threshold, target_diff_impedance, skew_tolerance_mm) |
| 07 | [Match-Group Test](07-matchgroup-test/) | ⚠️ Scaffold | 8 | 33 | Epic [#2661] Phase 3L regression bench --- DDR data byte, MIPI CSI, HDMI TMDS, address bus on 4-layer; exercises Phase 1A-2G match-group features (length_match_group, length_match_reference, length_match_tolerance_mm) |

**Status Legend:**
- ✅ Working - Generates manufacturable output
- ⚠️ Needs optimization - Works but may have routing challenges or require post-processing
- 🚧 Work in progress - Incomplete implementation

## Prerequisites for Manual Build

Before running board generation scripts directly, set up the development environment:

```bash
# From repository root
uv sync --extra dev
```

This installs kicad-tools and all dependencies. See the [main README](../README.md#development) for details.

## Advanced: Manual Build

For more control, you can run individual Python scripts directly:

```bash
# Run any board's generation script (from repo root)
uv run python boards/01-voltage-divider/generate_design.py

# Or activate the virtual environment first
source .venv/bin/activate  # Linux/macOS
python boards/01-voltage-divider/generate_design.py
```

### Post-Build Commands

After building, you can run additional verification and export commands:

```bash
# Check for DRC violations with manufacturer rules
kct check output/voltage_divider_routed.kicad_pcb --mfr jlcpcb

# Generate BOM
kct bom output/voltage_divider.kicad_sch --format csv

# Export Gerbers (via KiCad or kicad-cli)
# Open the .kicad_pcb file in KiCad and use File > Plot
```

See individual board READMEs for board-specific details.

## Routing Entry Point Conventions

When writing a new board's `generate_design.py`, pick the routing entry point
that matches the design's signal characteristics.  The recommended pattern,
derived from the architectural review in [#3072], is:

| Design has... | Recommended entry | Boards using this pattern |
|---|---|---|
| No differential pairs and no length-matching | `subprocess.run(["kct", "route", ...])` *or* in-process `router.route_all()` | 00, 01, 02, 04 |
| Differential pairs (USB2/USB3/PCIe/MIPI/HDMI/etc.) | `subprocess.run(["kct", "route", ..., "--differential-pairs"])` *or* in-process `router.route_all_with_diffpairs(diffpair_config=...)` | 03, 05, 06 |
| Match-groups (DDR, address buses) | Same as above; see `boards/07-matchgroup-test/` for the in-development pattern | 07 (work in progress) |

### Why this matters

`router.route_all()` (and `kct route` without `--differential-pairs`) run a
per-net A* loop that is unaware of `DifferentialPair` metadata.  On a board
with diff pairs, this path silently bypasses Phase A (`CoupledPathfinder`)
and Phase B (intra-pair clearance rip-up), producing routes that violate
the `intra_pair_clearance`, `coupled_continuity_threshold`, and
`target_diff_impedance` constraints declared in the project.

`router.route_all_with_diffpairs()` (and `kct route --differential-pairs`)
invoke the Phase A/B pipeline explicitly.  PR #3069 (board 03) and PR #3090
(board 06) migrated to this entry after the bypass bug was diagnosed.

> **✅ Recipe restored ([#3922]):** the #3308/#3410 recipe consolidation had
> silently dropped `--differential-pairs` from board 03's routing recipe
> (`generate_design.py:route_pcb()`), so the recipe no longer even *requested*
> diff-pair routing and this README's claim went false. The flag (plus
> `--net-class-map`, which forwards the D+/D- `intra_pair_clearance` metadata
> and engages the validate-side diff-pair DRC rules) is now restored and
> mirrored in `tests/router/test_board03_routing_baseline.py`, guarded by
> `test_recipe_includes_differential_pairs_flag()` so a future consolidation
> cannot drop it silently again.
>
> **Known limitation — runtime coupling gated on [#3952]:** on board 03 the
> flag is currently **inert at routing time**. The board's fine-pitch USB-C
> (J1) and QFP-32 (U1) force the CLI's escape-routing dispatch, and
> `route_cmd`'s escape / auto-layers-escalation paths do not consult
> `args.differential_pairs` — so the `CoupledPathfinder` pre-pass
> (`route_all_with_diffpairs`) never runs and the D+/D- skew remains
> coincidental rather than constructed. The recipe deliberately keeps
> `--auto-layers` (the default) because it preserves the escape pre-phase
> board 03 needs for **13/13 + 0 native DRC**; forcing the diff-pair path with
> `--no-auto-layers` *does* invoke Phase A but drops escape routing and
> reintroduces a `kicad-cli` clearance violation. Integrating diff-pair
> routing into the escape path (so Phase A runs *with* escape routing at 0
> DRC) is tracked in [#3952] (adjacent to the `CoupledPathfinder` convergence
> work in [#3921]). The behavior is pinned by an xfail'd
> `test_coupled_pathfinder_phase_a_invoked` that will flip to XPASS when
> [#3952] lands.

### Subprocess (`kct route`) vs. in-process (`router.route_all_*`)

Both are supported.  The subprocess path (`subprocess.run(["kct", "route", ...])`)
is preferred for new boards because:

- It is the production routing path; in-process tests can drift from it.
- CLI flags (`--strategy`, `--iterations`, `--differential-pairs`,
  `--net-class-map`, `--seed`) are validated end-to-end by CI.
- Zone-fill, post-route DRC, and routed-PCB artifact emission are handled
  automatically (see `route_cmd._fill_zones_after_route`).

The in-process path remains the right choice when a board needs to:

- Inject custom logic between routing and post-processing (e.g. board 06's
  custom diff-pair config plumbing).
- Exercise router internals that the CLI does not yet expose (e.g.
  `DifferentialPairConfig.per_pair_max_iterations`,
  `enable_shadow_construction`, or board 06's tightly-coupled width
  re-solve). Note: per-class values like `intra_pair_clearance` **do**
  survive the CLI's JSON sidecar round-trip (`kct route --net-class-map`,
  #2996) — an earlier version of this section claimed otherwise; that claim
  was verified false in the 2026-07-05 sweep.

### Architectural decision: auto-detect was rejected

[#3072] explicitly considered teaching `router.route_all()` (and the CLI's
default path) to auto-detect diff pairs and short-circuit to the
Phase-A/B-aware entry.  The decision was to **not** auto-detect, for two
reasons:

1. The remaining footgun is procedural (new boards forget the right entry).
   **Update 2026-07-05: the footgun bit** --- board 03 lost its
   diff-pair-aware entry in a later recipe consolidation without anyone
   noticing ([#3922]), exactly the failure mode auto-detect would have
   prevented. The flag was restored and a recipe-contract test now guards
   it ([#3922]); note the restored flag is still inert on board 03 until the
   route_cmd escape/diff-pair dispatch is fixed ([#3952]). The procedural
   footgun remains for the *next* new board — weigh this when re-promoting
   the auto-detect work.
2. An auto-detect change would have to land on both surfaces (in-process
   and CLI) to be complete, and the `CoupledPathfinder` latency issue
   tracked in [#3089] makes a CLI-default flip premature.

Re-promote the auto-detect work as a separate issue once [#3089] resolves
and a regression baseline exists for board 06's escape-time behavior.

[#3072]: https://github.com/rjwalters/kicad-tools/issues/3072
[#3089]: https://github.com/rjwalters/kicad-tools/issues/3089

## Known Issues

These are known limitations that may affect your experience:

| Issue | Affects | Description |
|-------|---------|-------------|
| [#659] | 02, 03 | Trace optimization may be needed for dense designs (02 now routes 8/8 as of 2026-07-05) |
| [#661] | All | Router doesn't always warn about DRC violations |
| [#3918] | All | `kct build` currently cannot complete on any demo board — use each board's `generate_design.py`/`design.py` recipe until the orchestrator fixes land |

[#659]: https://github.com/rjwalters/kicad-tools/issues/659
[#661]: https://github.com/rjwalters/kicad-tools/issues/661
[#3918]: https://github.com/rjwalters/kicad-tools/issues/3918
[#3921]: https://github.com/rjwalters/kicad-tools/issues/3921
[#3922]: https://github.com/rjwalters/kicad-tools/issues/3922
[#3952]: https://github.com/rjwalters/kicad-tools/issues/3952

## Project Files (.kct)

Each board includes a `project.kct` file with:

- **Project metadata**: Name, revision, author, description
- **Intent**: Use cases, interfaces, constraints
- **Requirements**: Electrical, mechanical, manufacturing specs
- **Suggestions**: Component choices, layout guidelines
- **Decisions**: Design choices with rationale
- **Progress**: Current phase and checklist

Example:
```yaml
kct_version: "1.0"

project:
  name: "Voltage Divider"
  revision: "A"
  artifacts:
    schematic: "output/voltage_divider.kicad_sch"
    pcb: "output/voltage_divider.kicad_pcb"

intent:
  summary: |
    Minimal test circuit for validating the complete
    kicad-tools pipeline.
```

## Board Details

### 01 - Voltage Divider (Simplest)

A minimal 4-component design for validating the complete kicad-tools workflow:
- 2 connectors (input/output)
- 2 resistors (10k/10k divider)
- 5V → 2.5V conversion

**Demonstrates**: Schematic generation, PCB creation, autorouting, DRC checking

### 02 - Charlieplex LED (Medium)

3x3 LED matrix using charlieplexing to drive 9 LEDs with 4 GPIO pins:
- Dense interconnected topology
- Many crossing nets requiring vias
- Intentionally challenging routing

**Demonstrates**: Monte Carlo routing, congestion handling, dense layouts

### 03 - USB Joystick (Complex)

USB HID game controller with mixed signal types:
- USB Type-C with differential pairs
- Analog joystick inputs
- Digital button inputs
- ATmega32U4 MCU

**Demonstrates**: Mixed-signal routing, impedance requirements, complex placement

### 04 - STM32 Dev Board (Most Complex)

STM32F103 "Blue Pill" style development board:
- Power regulation (USB → 3.3V LDO)
- Crystal oscillator
- SWD debug interface
- User LED

**Demonstrates**: Programmatic schematic generation with circuit blocks

### 06 - Differential Pair Test (Routing Testbench)

Multi-protocol HSDI regression testbench:
- USB 2.0 D+/D- pair (USB-C → QFN-32)
- USB 3.0 SuperSpeed (4 pairs: USB-C → BGA-49 simulator)
- PCIe Gen1 (2 pairs: mini-PCIe edge → QFP-48)
- MIPI D-PHY (2 lanes: FFC → QFN-24)
- 4-layer JLCPCB tier-1 stackup (F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu)

**Demonstrates**: Epic [#2556] Phase 1-3 differential-pair subsystem (per-class
`intra_pair_clearance`, `coupled_routing`, `coupled_continuity_threshold`,
`target_diff_impedance`, `target_single_impedance`, `skew_tolerance_mm`).
Not a working device --- synthetic sinks drive routing exercise only.

[#2556]: https://github.com/rjwalters/kicad-tools/issues/2556

### 07 - Match-Group Test (Routing Testbench)

Multi-protocol N-trace match-group regression testbench:
- DDR data byte (10 nets: DQ0-7 + DM0 + DQS_P/N pair) -- N-trace + diff-pair group composition
- MIPI CSI lanes (3 pairs: CLK + DAT0 + DAT1)
- HDMI TMDS lanes (3 pairs: D0 + D1 + D2)
- Address bus A0-A7 (single-ended N-trace group)
- 4-layer JLCPCB tier-1 stackup (F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu)

**Demonstrates**: Epic [#2661] Phase 1-2 match-group routing subsystem
(per-class `length_match_group`, `length_match_reference`,
`length_match_tolerance_mm` + diff-pair `skew_tolerance_mm` for pair
members within a group).  Not a working device --- synthetic
sources/sinks drive routing exercise only.

[#2661]: https://github.com/rjwalters/kicad-tools/issues/2661

## See Also

- [examples/](../examples/) - Feature-specific demos (BOM, DRC, placement, etc.)
- [examples/04-autorouter/](../examples/04-autorouter/) - Routing strategy comparisons
