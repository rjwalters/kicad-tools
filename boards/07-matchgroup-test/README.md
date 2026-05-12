# Match-Group Test Board (Board 07)

Regression testbench for the N-trace match-group routing subsystem
introduced by Epic [#2661](https://github.com/rjwalters/kicad-tools/issues/2661)
(Phases 1-2).  Demonstrates each match-group scenario the epic was
scoped against (DDR data byte, MIPI CSI lane group, HDMI TMDS lane
group, generic address bus) by routing one or more match-grouped
nets per scenario on a 4-layer JLCPCB tier-1 stackup.

**This is not a working device.**  The source / sink footprints
(QFN-48, BGA-49 simulator, FFC-6, etc.) are synthetic placeholders
chosen to expose realistic escape lengths so the routing algorithm
sees non-trivial group skew to measure / tune.

## Quick Start

```bash
# One-command build (recommended)
kct build boards/07-matchgroup-test

# Or run specific steps
kct build boards/07-matchgroup-test --step schematic
kct build boards/07-matchgroup-test --step pcb
kct build boards/07-matchgroup-test --step route
kct build boards/07-matchgroup-test --step verify

# Run DRC against the committed routed PCB
kct check boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb \
  --mfr jlcpcb \
  --net-class-map boards/07-matchgroup-test/output/net_class_map.json
```

## Stackup

4-layer JLCPCB tier-1 (identical to board 06):

| Layer index | KiCad name | Purpose                              |
|-------------|-----------|--------------------------------------|
| 0           | F.Cu      | Signal (escape + outer routing)      |
| 1           | In1.Cu    | GND plane (impedance reference)      |
| 2           | In2.Cu    | PWR plane (+1V2 / +1V8)              |
| 31          | B.Cu      | Signal (optional bottom escape)      |

## Match-Group Scenarios

| Scenario   | Group name           | Members                     | Skew tolerance | Phase features exercised                                                  |
|------------|----------------------|-----------------------------|----------------|---------------------------------------------------------------------------|
| DDR byte   | `DDR_DATA_BYTE_0`    | 9 singles (DQ0-7, DM0) + DQS pair | 0.1 mm    | 1A declaration, 1B tracker, 1C detection, 1D producer, 2E cascade, 2F composition |
| MIPI CSI   | `MIPI_CSI_LANES`     | 3 pairs (CLK, DAT0, DAT1)   | 0.05 mm        | 1A, 1B, 1C, 1D, 2F symmetric serpentine target                            |
| HDMI TMDS  | `HDMI_TMDS_LANES`    | 3 pairs (D0, D1, D2)        | 0.075 mm       | 1A, 1B, 1C, 1D, 2F composition                                            |
| ADDR bus   | `ADDR_BUS`           | 8 singles (A0-A7)           | 0.5 mm         | 1A explicit declaration (also exercises 1C suffix-inference fallback path) |

**4 match groups across 30 paired/grouped nets**.  Combined with
GND/+1V2/+1V8, the board has **33 nets total** --- comparable in
scale to board 06 (26 nets) and well below board 05 (~50 nets).

## Components

| Reference | Description                              | Footprint                                          |
|-----------|------------------------------------------|---------------------------------------------------|
| U1        | QFN-48, 0.8 mm pitch (DDR controller)    | `Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm`         |
| U2        | QFN-48, 0.8 mm pitch (DRAM sink)         | `Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm`         |
| J1        | FFC, 6-pin 1.0 mm pitch (MIPI source)    | `Connector_FFC:FFC_6P_1.0mm`                      |
| U3        | QFN-24, 0.8 mm pitch (MIPI sink)         | `Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm`         |
| J2        | HDMI Type A receptacle (synthetic)       | `Connector_Video:HDMI_A_Receptacle`               |
| U4        | BGA-49 simulator, 1.27 mm pitch          | `Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm`   |
| J3        | 1x9 pin header, 0.1 in pitch (ADDR)      | `Connector_PinHeader_2.54mm:PinHeader_1x09_P2.54mm_Vertical` |
| U5        | QFP-48, 0.8 mm pitch (SRAM-style sink)   | `Package_QFP:LQFP-48_7x7mm_P0.5mm`                |

All sinks placed on F.Cu so all routing happens on the outer signal
layers; GND/PWR planes on inner layers are unencumbered for
impedance reference.

## Per-Group Net Class Declarations

The protocol-specific `NetClassRouting` instances live in
`generate_design.py`:

- `ddr_data_byte_0_net_class()` --- `length_match_group="DDR_DATA_BYTE_0"`,
  `length_match_tolerance_mm=0.1` (DDR commodity tolerance)
- `ddr_dqs_pair_net_class()` --- pair members of DDR group
  + `coupled_routing=True`, `skew_tolerance_mm=0.05` (Phase 3H within-pair)
- `mipi_csi_net_class()` --- `length_match_group="MIPI_CSI_LANES"`,
  `target_diff_impedance=100`, `skew_tolerance_mm=0.05`
- `hdmi_tmds_net_class()` --- `length_match_group="HDMI_TMDS_LANES"`,
  `target_diff_impedance=100`, `skew_tolerance_mm=0.075`
- `addr_bus_net_class()` --- `length_match_group="ADDR_BUS"`,
  `length_match_reference="A0"` (pace-car semantic)

`build_net_class_map()` assembles them into a `net_name -> NetClassRouting`
dict that the autorouter (in `route_pcb()`), the JSON sidecar
(`output/net_class_map.json`), AND the regression test
(`tests/test_board_07_matchgroup_test.py::test_phase_features_exercised`)
all import.  This ensures test/sidecar/implementation parity.

## Files

| File                                           | Description                                           |
|------------------------------------------------|-------------------------------------------------------|
| `project.kct`                                  | KCT v1.0 spec (manufacturing/intent metadata)         |
| `generate_schematic.py`                        | Emits the schematic (`output/matchgroup_test.kicad_sch`) |
| `generate_pcb.py`                              | Emits the unrouted PCB + holds NETS / DIFFPAIRS / match-group dicts |
| `generate_design.py`                           | End-to-end pipeline (schematic + PCB + route + sidecar + DRC) |
| `output/matchgroup_test.kicad_sch`             | Generated schematic (committed)                       |
| `output/matchgroup_test.kicad_pcb`             | Unrouted PCB (committed)                              |
| `output/matchgroup_test_routed.kicad_pcb`      | Routed PCB (committed --- consumed by CI DRC gate)    |
| `output/net_class_map.json`                    | Sidecar (Phase 3M) consumed by `kct check --net-class-map` |

## DRC Status

The routed PCB is checked against JLCPCB tier-1 rules via:

```bash
kct check output/matchgroup_test_routed.kicad_pcb \
  --mfr jlcpcb \
  --errors-only \
  --net-class-map output/net_class_map.json
```

The `--net-class-map` flag is **mandatory** for the
`match_group_length_skew` rule (Phase 2G #2702) and the diff-pair
rules to fire on the standalone routed PCB --- without the sidecar
they degrade to no-ops (the same trap PR #2692 fixed for diff-pair
rules).

### Initial allowlist budget

Per the curator note on issue #2724, the initial DRC error count is
empirical: route the board, count errors, add ~20% headroom, then
record in `.github/routed-drc-tolerance.yml`.  Phase 3N (#2726) will
tighten iteratively as upstream router improvements land.

## Phase 3 Dependency: #2723 (`--length-match-groups`)

The `apply_match_group_tuning` orchestrator + `--length-match-groups`
CLI flag are introduced by **Phase 3H (#2723)** and have NOT yet
landed on `main` at the time of this board's scaffolding.

What works today (Phases 1A/1B/1C/1D + 2.5G):

- `length_match_group` net-class field is declared on every group
- `detect_match_groups` consumes the declarations during routing
- `_finalize_routing` populates `MatchGroupTracker` with measured
  per-group skew (post-route)
- `match_group_length_skew` DRC rule fires when the sidecar JSON
  is loaded into `kct check`

What is deferred until #2723 lands:

- Group-level meander insertion to actively REDUCE skew (only
  measurement happens today)
- AC#7's "post-pass skew strictly less than pre-pass skew" check
  for the DDR data byte (today's pre-pass and post-pass skews
  are identical because no tuning step runs)

When #2723 lands, the `route_pcb` function's `# TODO Phase 3H
(#2723): apply_match_group_tuning(router, ...)` marker should be
replaced with the actual call.

## CI Gate (Phase 3N, #2726)

This board is consumed by the **Phase 3N CI gate** (issue #2726),
which lives at `.github/workflows/ci.yml::matchgroup-routing-regression`.
The gate is a sibling of the `diffpair-routing-regression` job (board
06) and runs on every PR to `main`.  Contract:

1. Re-routes board 07 from scratch with
   `python boards/07-matchgroup-test/generate_design.py --step route --seed 42`.
2. Asserts the resulting DRC error count is within the per-board
   allowlist in `.github/routed-drc-tolerance.yml` (currently 80).
3. Asserts the `match_group_length_skew` DRC rule was actually
   exercised --- i.e., `rules_checked_by_rule["match_group_length_skew"] >= 1`
   in the `DRCChecker` summary.  Without this assertion a regression
   that disables match-group detection (e.g., a future change that
   breaks `derive_group_skew_data` or unwires `length_match_group`
   from the net classes) would silently produce a 0-violation report
   and hide the defect.

### Interpreting a failure

- **`Match-group rule(s) NOT exercised`** -- the rule short-circuited
  because no declared groups were detected with measurable skew.
  Likely causes: `length_match_group` field unwired from one or more
  net classes in `build_net_class_map`, `derive_group_skew_data`
  broken (e.g., `detect_match_groups` returning empty), or the
  routed PCB has no traces matching any declared group's members.
  See `src/kicad_tools/validate/match_group_skew.py` and
  `src/kicad_tools/validate/rules/match_group_length_skew.py` for
  the engagement conditions.
- **`DRC regression on re-routed ...`** -- the routing algorithm
  produced more errors than the allowlist value.  Fix the router
  regression OR (with reviewer sign-off) raise the allowlist value
  in the same PR.
- **Job times out (> 10 min)** -- routing wall-clock crept above
  the budget.  File a follow-up to move the gate to a nightly
  schedule per Epic #2661's runtime guidance.

### Reproducing the gate locally

```bash
# Full re-route + check (CI semantic, ~30-90s on a developer laptop)
python scripts/ci/check_matchgroup_coverage.py boards/07-matchgroup-test --seed 42

# Fast iteration against the committed routed PCB (skips the route step)
python scripts/ci/check_matchgroup_coverage.py boards/07-matchgroup-test \
  --seed 42 --skip-route
```

### Branch-protection (admin action required post-merge)

Per the precedent set by `diffpair-routing-regression` (PR #2679),
adding `Match-Group Routing Regression` to the list of *required*
status checks on `main` is a separate repo-admin action in GitHub
Settings -> Rules -> Rulesets.  Until that flip happens the job
runs on every PR but is advisory --- its failure does not block
the merge button.

## Out of Scope

Per issue [#2724](https://github.com/rjwalters/kicad-tools/issues/2724)
Scope (out):

- **No new router / validate features.** This board EXERCISES
  Phases 1-2; it doesn't add new router logic.  Missing-feature
  gaps file separate issues under #2661.
- **No reference circuits.** No actual DDR controller, no real
  MIPI camera. Connector / package -> breakout footprint is enough.
- **No 5th scenario** (USB 3.0, SDIO, etc.). Scope is fixed at 4.
- **No CI matrix wiring.** Phase 3N (#2726) does that wiring.
- **No modification of boards 01-06.** Purely additive.

## Related Issues

- Epic: [#2661](https://github.com/rjwalters/kicad-tools/issues/2661) (N-trace match-group routing subsystem)
- This issue: [#2724](https://github.com/rjwalters/kicad-tools/issues/2724) (Phase 3L scaffolding)
- Blocked by: [#2723](https://github.com/rjwalters/kicad-tools/issues/2723) (Phase 3H CLI flag + orchestrator)
- Blocks: [#2726](https://github.com/rjwalters/kicad-tools/issues/2726) (Phase 3N CI integration)
- Documentation: Phase 3M will cite this board's `project.kct` + net-class declarations as canonical examples.
