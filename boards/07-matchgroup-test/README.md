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

## Routing Plateau (5 seed-invariant opens)

**Status: intentional, evidence-backed plateau.**  Board 07 routes
**26/31 signal nets** (~83.9% by net count); the committed
`output/lvs.json` honestly records `clean: false` with exactly **5 open
copper mismatches**.  This is a genuine placement/topology limit, not an
untracked gap and not a router-knob that has been left unturned.  The
gallery renders board 07 as `partial`/`lvs` for exactly this reason, and
that chip is the intended honest state (the audit is real:
`copper_bound_pad_count: 244`, `copper_vacuous: false` --- cf. #4011).

### The 5 opens

| Net | Pad A | Pad B | Bundle |
|-----|-------|-------|--------|
| `DQ3`         | U1.28 | U2.4  | DDR data byte |
| `DQ4`         | U1.32 | U2.8  | DDR data byte |
| `MIPI_DAT0_N` | J1.4  | U3.4  | MIPI CSI |
| `TMDS_D0_N`   | J2.2  | U4.B2 | HDMI TMDS |
| `TMDS_D1_N`   | J2.5  | U4.B4 | HDMI TMDS |

This is the same set re-measured by the #4049 epic dossier; the older
#3438 thread cited a 3-net subset before the board was re-spun.  Three
verifiers agree on this exact set: `kct net-status --why`,
`output/lvs.json` (`copper_mismatches`), and the KiCad cross-gate
`kicad-cli pcb drc --refill-zones` (5 unconnected pads at the same five
pads).

### Fresh per-net verdict (`kct net-status --incomplete --why`)

Measured on the committed routed PCB and reproduced by a from-scratch
seed-42 re-route (`PYTHONHASHSEED=42 generate_design.py --step route
--seed 42`), which lands on the **identical** 5-open set and the same
classification:

```
Stuck signal nets: 5
  ESCAPE_BLOCKED:       0
  CONGESTION_SATURATED: 2
  BUDGET_STARVED:       0
  PLACEMENT_BOUND:      3
  POUR_DISCONTINUOUS:   0
```

| Net | Verdict | Evidence |
|-----|---------|----------|
| `TMDS_D0_N`   | `CONGESTION_SATURATED` | pad reachable (360 deg lane) but boxed in by committed strict copper (nearest blocker 1.000mm); ripping it would strand `TMDS_D0_P`, `TMDS_D1_P` (1:1 trade) |
| `TMDS_D1_N`   | `CONGESTION_SATURATED` | boxed in (nearest blocker 0.975mm); rip would strand `TMDS_D1_P`, `TMDS_D2_P` (1:1 trade) |
| `DQ3`         | `PLACEMENT_BOUND` | dense cluster (24 foreign obstructions, nearest strict blocker 0.800mm) --- a part must move |
| `DQ4`         | `PLACEMENT_BOUND` | dense cluster (30 foreign obstructions, nearest strict blocker 0.047mm) --- a part must move |
| `MIPI_DAT0_N` | `PLACEMENT_BOUND` | dense cluster (39 foreign obstructions, nearest strict blocker 0.800mm) --- a part must move |

The key signal: **0** nets are `BUDGET_STARVED` (which more router
iterations/wall-clock would fix) or `ESCAPE_BLOCKED` (which corridor
reservation would fix).  Every stuck net is either the diagnostic's own
"a part must move" `PLACEMENT_BOUND` verdict (3/5) or a
`CONGESTION_SATURATED` 1:1 trade (2/5) where ripping the stuck net simply
strands its diff-pair partner --- the N-1 invariant #3438 first
documented.

### What was attempted this pass (v0.16.0 router)

A fresh seed-42 route with the current v0.16.0 router (region-bounded
routing, ampacity net-class, and the post-#4049 hardening all now on
`main`, C++ backend built) was run end-to-end.  The negotiated router's
relief-rescue phase explicitly reported **`Relief rescue resolved 0/5
net(s)`**: for each stuck net it either found no relief path, or found a
path that displaced victims it could not re-land (rolled back under the
no-net-loss guarantee).  The best-so-far routed state (26/31, 0 clearance
violations, 0 overflow) is identical in open-set to the committed
artifact, so the committed artifact remains the shipping truth and is
left unchanged (regen diverges only in trace geometry/UUIDs, not in which
nets close).

### Track A bundle-plan allocator — measured seed-42 verdict (#4257, A4)

Track A (#4252) built the "joint bundle corridor allocation" fix named
below as the genuine remedy for the `CONGESTION_SATURATED` TMDS 1:1
trades: an **atomic diff-pair rip-up/relief transaction** (A2, #4255) so a
committed "P-routed / N-stranded" state is unrepresentable, plus a
**discrete `BundlePlan` allocator with HARD (foreign-net keep-out) per-member
lane reservation** (A3, #4256).  A4 (#4257) wired both through all three
negotiated entry points (`route_all`, `route_all_negotiated`,
`TwoPhaseRouter`) behind the default-OFF `--bundle-river-planner`
(`enable_bundle_river_planner`) flag and ran the board-07 acceptance gate.

**Honest outcome: the TMDS opens do NOT close; the residual is
placement-bound.**  A solo seed-42 A/B on one host (C++ backend built,
`--deterministic-budget`, `PYTHONHASHSEED=42`) measured:

| Run | Reach | Open set |
|-----|-------|----------|
| flag **OFF** (default / shipping) | **26/31** | `DQ3`, `DQ4`, `MIPI_DAT0_N`, `TMDS_D0_N`, `TMDS_D1_N` |
| flag **ON** (`--bundle-river-planner`) | **23/31** | the 5 above **+** `MIPI_CLK_N`, `TMDS_D0_P`, `TMDS_D1_P` |

The reason is the load-bearing A3 finding, now re-confirmed against the
committed placement: the allocator returns the HDMI TMDS bundle
**FEASIBLE with 6 trivial in-order OUTER lanes and 0 via-hops** — the two
facing rows (J2 / U4) carry `D0..D2` co-oriented, so there is **no
forced-crossing contention for the allocator to resolve**.  Reserving the
resulting HARD lanes (12 keep-out lanes / 3780 grid cells across the TMDS
and MIPI bundles) therefore does not open the stuck TMDS N-legs; it only
walls off cells the congested single-ended negotiator needed, stranding
the *partners* (`TMDS_D0_P`, `TMDS_D1_P`, `MIPI_CLK_N`) — a **regression**,
not a gain.

Conclusion: `TMDS_D0_N` / `TMDS_D1_N` are **`PLACEMENT_BOUND`**, not
coupling-contention-bound.  No HARD-lane reservation closes them; the
residual is the same "a part must move" limit as `DQ3` / `DQ4` /
`MIPI_DAT0_N`, tracked under **Track B / #4253** (placement rework).  The
board-07 reach is therefore **unchanged at 26/31** and **the committed
routed artifact is not refreshed** (the flag stays OFF by default and
flag-off is byte-identical to pre-A4 `main`).  Track A still ships its
real deliverable — the general, verified coupled-bundle capability (atomic
pair transaction + discrete allocator + HARD lane reservation, exercised
end-to-end from all three negotiated entry points in
`tests/router/test_bundle_plan_three_path_integration.py`) — available for
future geometries where a bundle *is* crossing-contended, behind the
default-OFF flag so production routing is unperturbed.

### Required class of fix (follow-up, out of scope here)

None of the four gaps is a knob the single-ended negotiated router can
turn today:

- **`PLACEMENT_BOUND` (DQ3, DQ4, MIPI_DAT0_N)** --- the diagnostic's
  verdict is literally "a part must move."  Resolving these requires a
  **placement rework** of the U1/U2/U3/U4/J1/J2 cluster, a separate and
  larger design change (out of scope per this board's charter; see
  #4049).
- **`CONGESTION_SATURATED` 1:1 trades (TMDS_D0_N, TMDS_D1_N)** ---
  ripping the stuck net strands its pair partner.  The genuine fix is
  **joint bundle corridor allocation** (route the whole HDMI/DDR/MIPI
  bundle's escape channel as a coupled group rather than net-by-net),
  the crossing-aware river-routing planner explored and deferred in
  **#3673**, and the CoupledPathfinder budget rework noted under #4049.

The exhaustive knob matrix (iterations, seed, monte-carlo,
differential-pairs, micro-via-in-pad, grid 0.1, two-pass pre-route,
targeted-ripup) was already swept to exhaustion in **#3438** without
reaching 31/31; the 11-net DDR bundle fails 2/11 *alone* on an empty
board.  The board-07 router epic **#4049** re-confirmed this and closed
at plateau.  This pass (#4235) re-validates that verdict against
v0.16.0.  The joint-corridor + placement-rework follow-up is tracked in
**#4243**.

Gap 2 (diff-pair length-skew / routing-continuity quality errors on the
pairs that *are* routed --- DQS/MIPI_CLK/MIPI_DAT1/TMDS_D2) is a
different root cause (pairs routed as independent single-ended traces),
tracked under #4049, and is deliberately **not** touched here.

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
