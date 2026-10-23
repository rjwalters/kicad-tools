> Historical synthetic routing fixture. These footprints are not an assembled
> product. The working SDRAM redesign is in `../real_design/`. Legacy generators
> remain in the parent directory and default to `../regression-output/`; the
> files here preserve the existing regression witness.

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

# Run DRC against the committed routed PCB (the fixture in this directory)
kct check boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb \
  --mfr jlcpcb \
  --net-class-map boards/07-matchgroup-test/regression-fixture/net_class_map.json
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
  + `coupled_routing=True`, `pair_protocol="DDR_DQS_CK"` (UG583 2 ps
  within-pair limit, an error), `skew_tolerance_mm=0.05`
- `mipi_csi_net_class()` --- `length_match_group="MIPI_CSI_LANES"`,
  `target_diff_impedance=100`, `pair_protocol="MIPI_DPHY"`, `skew_tolerance_mm=0.05`
- `hdmi_tmds_net_class()` --- `length_match_group="HDMI_TMDS_LANES"`,
  `target_diff_impedance=100`, `pair_protocol="HDMI_TMDS"`, `skew_tolerance_mm=0.075`
- `addr_bus_net_class()` --- `length_match_group="ADDR_BUS"`,
  `length_match_reference="A0"` (pace-car semantic)

Within-pair skew is judged against the derived ps budget of the declared
protocol ([guide 09](../../../docs/guides/diff-pairs/09-skew-budget.md)),
plus the explicit `skew_tolerance_mm` caps.  The caps stay until #7913:
without them the seed-42 re-route fails `impedance` on re-routed TMDS
breakout runs (Issue #7894).

`build_net_class_map()` assembles them into a `net_name -> NetClassRouting`
dict that the autorouter (in `route_pcb()`), the JSON sidecar
(`regression-fixture/net_class_map.json`), AND the regression test
(`tests/test_board_07_matchgroup_test.py::test_phase_features_exercised`)
all import.  This ensures test/sidecar/implementation parity.

## Files

Paths below are relative to `boards/07-matchgroup-test/`.  The legacy
generators live in that parent directory; the committed regression
witnesses live here, in `regression-fixture/`.  (`output/` holds the
unrelated real SDRAM-exerciser build --- see `../real_design/`.)

| File                                                   | Description                                           |
|--------------------------------------------------------|-------------------------------------------------------|
| `regression-fixture/project.kct`                       | KCT v1.0 spec (manufacturing/intent metadata)         |
| `generate_schematic.py`                                | Emits the schematic (`matchgroup_test.kicad_sch`)     |
| `generate_pcb.py`                                      | Emits the unrouted PCB + holds NETS / DIFFPAIRS / match-group dicts |
| `generate_design.py`                                   | End-to-end pipeline (schematic + PCB + route + sidecar + DRC); regenerates into `regression-output/` by default, never over the committed fixture |
| `regression-fixture/matchgroup_test.kicad_sch`         | Generated schematic (committed)                       |
| `regression-fixture/matchgroup_test.kicad_pcb`         | Unrouted PCB (committed; the gate's re-route input --- carries #7557's U4 TMDS ball move, see "U4 TMDS ball move" below) |
| `regression-fixture/matchgroup_test_routed.kicad_pcb`  | Routed PCB (committed --- consumed by CI DRC gate)    |
| `regression-fixture/net_class_map.json`                | Sidecar (Phase 3M) consumed by `kct check --net-class-map` |
| `regression-fixture/placement_delta.json`              | Reviewed swap-group pad re-binding (#5537); replayed only via `--placement-delta` |

## DRC Status

The routed PCB is checked against JLCPCB tier-1 rules via:

```bash
kct check boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb \
  --mfr jlcpcb \
  --errors-only \
  --net-class-map boards/07-matchgroup-test/regression-fixture/net_class_map.json
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
tighten iteratively as upstream router improvements land.  (#7367
replaced that count with an identity allowance; see "CI Gate" below.)

## Current targeted repair (2026-09-09)

The committed routed board connects **27/31 signal nets**, including all six
MIPI nets. It remains **partial and unsuitable for manufacturing sign-off**:
`DQ3`, `DQ4`, `TMDS_D0_N`, and `TMDS_D1_N` are open. Native KiCad DRC, with
refilled zones and the original project/rules, reports four unconnected items,
zero other errors, and 31 warnings. `kct check` reports eight errors: those four
opens and the remaining DDR/HDMI pair skew and continuity failures. All three
MIPI pairs and their match group pass the authored electrical checks.

The repair rotates J1 to -90 degrees at the same location, reroutes only MIPI,
and preserves the other copper. MIPI and HDMI width is resolved to 0.225 mm at
the authored 0.100 mm gap (102.3 ohm model, within the authored 100 ohm ±10%
target). DQS remains at its authored 0.150/0.100 mm width/gap. No net mappings,
pair/group constraints, or checks were removed; no waivers were added.

Reproduce the targeted repair in a separate directory:

```sh
uv run python boards/07-matchgroup-test/repair_mipi.py /tmp/board07-repair
```

The script accepts `--input PATH` for a previous routed artifact, sets J1's
absolute orientation, disables the routing cache, runs coupled MIPI routing,
then tunes pair skew and a common six-route length target while enforcing
partner clearances. It saves the full sidecar, tuning results, route log,
native DRC, and full `kct check` report. Exit status 2 reflects the board's
remaining errors. The generator exposes this J1 orientation as an optional
argument; its default placement and routing map stay historical because a fresh
full-board generation/routing run has **not** been measured with this change.
Fixed-gap width sizing is explicitly selected by the repair script;
the committed result comes from the targeted repair of the historical board.
See [the experiment record](../diagnostic-runs/astra-j1-orientation.md).

## Historical routing plateau (before 2026-09-09 repair)

The following measurements describe the previous committed artifact and the
earlier full-board routing recipe. That board connected
**26/31 signal nets** (~83.9% by net count); its
`output/lvs.json` recorded `clean: false` with exactly **5 open
copper mismatches**. The gallery rendered board 07 as `partial`/`lvs`
(the audit was real:
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

### Decision: correct HDMI widths while preserving the witness (#5126)

**Recommendation:** permit one explicit, reviewed width correction to this
historical fixture. Retarget existing `TMDS_*` track widths from 0.375 mm to
`HDMI_TMDS_LANES`' declared 0.225 mm, preserving their centerlines, layers,
net assignments and vias. The fixture exists to retain match-group skew geometry
and documented routing defects; a stale width contradicting its own sidecar is
not an additional defect we need to preserve. This does not make the synthetic
board a release artifact or remove its known opens.

The September 12 investigation on #5126 reported matching before/after DRC
error signatures after native refill and an unchanged angle census of 841
segments, including 12 off-angle segments. It also reported that stripping and
rerouting all six TMDS nets reduced their connected count from four to one.
Those are prior experimental results, not acceptance evidence for a future
commit. **A full strip-and-reroute is not proposed.** The present saved fixture
has 0.375 mm segments on the four routed TMDS nets; the two documented open
TMDS nets have no routed segments to retarget.

The alternative is to freeze every byte forever. That preserves the original
hash but leaves the fixture internally inconsistent and teaches readers the
wrong relationship between the sidecar and saved copper. A narrowly documented
exception with measured invariants better preserves the witness's purpose.
The original bytes remain identifiable by SHA256
`ab3a2c2d4aea466f828e540189ac851ddb8c41505c8185be65924ec5ff92a9a6`
and by repository history.

**Implementation gate:** after review of this decision, a separate #5126 PR
must add the width-retarget path to `repair_hdmi.py` and publish its exact
before/after evidence. It must preserve non-TMDS track geometry, all pad/net
identities, vias and authored constraints; `DDR_DQS` is outside this change.
Before promotion, compare native and analyzer pad components and open identities,
not just aggregate counts, and compare DRC error signatures with the original
project/rules and sidecar after native KiCad 10 zone refill. Narrower tracks can
lose a marginal copper contact even when their centerlines are unchanged, so
connectivity must be measured rather than assumed. Retain the original input,
refilled candidate, commands, KiCad identity and hashes for reproduction.
The known-current four-open set above must not be confused with the five-open
historical table, which predates the MIPI repair.

Update `HISTORICAL_WITNESS_SHA256` in `tests/test_fleet_45_census.py` only in the
same implementation commit as the accepted PCB, with a comment citing #5126.
Keep the exact `(841, 12)` angle-census assertion and exercise the match-group
skew checks; do not remove the witness pin or relax its regression gates.
This decision note changes neither PCB bytes nor their hash pin. If independent
review rejects the exception, retain the frozen witness and close #5126 with
that recorded rationale.

### HDMI width correction (2026-09-15, #5126)

The reviewed exception above is applied to **35 width atoms** on the four routed
TMDS nets: `TMDS_D0_P`, `TMDS_D1_P`, `TMDS_D2_N`, and `TMDS_D2_P`, from 0.375 mm
to the sidecar's 0.225 mm. The two open TMDS nets have no track segments.
Every other byte of the saved PCB is unchanged, including segment centerlines,
vias, existing zone fills, and non-TMDS widths. DQS copper is unchanged; this
correction does not establish its agreement with its authored width.

The old PCB SHA256 is recorded in the decision above; the corrected saved PCB is
`fb58581eba53bb9f1dfe33a278fb896b2f08b5f4fc2e82211124d8586ba7aaf0`.
Its hash pin was updated with the PCB, retaining the exact 841-segment,
12-off-angle census. The change is a regression-fixture correction, not a
manufacturing release.

Current-source before/after checks retain the same four open nets and exact
native pad components. Native KiCad 10.0.6 reports four unconnected items,
zero other errors and 31 warnings on each input with native refill enabled.
The current Python checker reports **13 errors, 22 warnings and 12 unique
error rule/net signatures** on both saved inputs, including existing
via-in-pad findings. These are current measurements; the earlier eight-error
account above describes the September 9 check. Match-group checks remain
engaged, and no error severity or allowance was changed.

The [retained component and signature record](hdmi-width-correction-5126.json)
contains the shared exact pad partitions/open identities and all four measured
PCB hashes. Saved and separately refilled before/after copies pass the same
comparisons. Native refills are evidence copies; the committed PCB retains its
original fill bytes so unrelated serialization and geometry remain untouched.

Reproduce from the original fixture in a fresh directory (use a Python with
`pcbnew` for `--native-python`, and a KiCad 10 `kicad-cli` on `PATH`):

```sh
mkdir /tmp/board07-original
git archive bcaf28ad3399d97df5b0d6cfefe8db7856abc689 \
  boards/07-matchgroup-test/regression-fixture | tar -x -C /tmp/board07-original
uv run python boards/07-matchgroup-test/repair_hdmi.py /tmp/board07-hdmi-width \
  --input /tmp/board07-original/boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb \
  --native-python python3
```

A completed preservation check reports `validated historical width correction`
in `evidence.json` and exits **2**, retaining the board's existing errors.
Failure raises an error and records `FAILED`; it is not a promotable candidate.
The script retains the original input, byte-minimal candidate, separate native
refills, reports and command logs. It also supports an already-correct input
without further width edits. The current script was verified from the original
fixture; the contact-loss negative test separately proves that a narrower
track losing a real connection is rejected.

### ADDR_BUS retune (2026-10-18, #6730)

`kct retune-lengths --group ADDR_BUS` re-applied the existing match-group tuner
to the saved fixture (only `A4` and `A6` changed; all other nets, vias and
pads are unchanged). ADDR_BUS skew went from **1.0998 mm to 0.0169 mm** (limit
0.5 mm), measured by `kct check` and by `kicad-cli` per-net lengths
(`test_addr_bus_lengths_match_kicad_cli`). The new meanders sit on In1.Cu, so the
zones were refilled with `kct zones fill`; without the refill the stale GND fill
reports six `clearance_segment_zone` errors. Error signatures are otherwise those
of the previous fixture minus the ADDR_BUS skew finding (`diffpair_length_skew` 5,
`via_in_pad` 5, `connectivity` 4). The census is now `(849, 12)` and the PCB
SHA256 is `fdbb69fd1cae5ee76a6a66788feebfed3490f090652df75fdbf04e864f3df83e`.
The input hash and per-net before/after lengths were recorded by the retuner's
provenance record. Earlier sections describe the pre-retune bytes.

### Placement-delta feedback (#4468, epic #3438 Phase 3)

The route step runs `kct route --placement-delta-feedback
--placement-delta-feedback-budget 2 --placement-delta-feedback-timeout
600` (wired from the `PLACEMENT_DELTA_FEEDBACK*` constants at the top of
`generate_design.py`).  Since #5266 the surrounding budget contract is
stated as two separate numbers: `--search-timeout 600` bounds each
individual search stage (the initial negotiated pass), while `--timeout`
carries the **hard total** invocation deadline, derived as
`600 + 2x600 + 600 = 2400 s` (one search stage, one probe allocation per
budget unit, and a postprocessing reserve).  The probe allocation does
**not** escape the total deadline -- it is clamped to whatever the total
has left -- so the total must be large enough to contain both probes or
the out-of-process supervisor kills the run mid-probe and leaves only
unverified partial artifacts.  Each iteration classifies the **routed**
board,
translates every `PLACEMENT_BOUND` / `CONGESTION_SATURATED` diagnosis into
one concrete placement delta, applies the top unprobed one, re-routes, and
keeps it only when the re-route **strictly increases the routed-net count
AND does not increase the router's clearance-violation count** ---
otherwise placement, router pads, routes and the routing grid revert
atomically.

**Measured verdict: the reach stays at 26/31, and that is a decision, not
a failure to find anything.**  Solo, seed 42, C++ backend built,
`PYTHONHASHSEED=42`, the loop emits a delta for every one of the 5 open
nets and probes the top two.  The probe-level routed counts are
**host-specific** (see "Host-vs-CI divergence" below); both are recorded:

| # | Probe | Routed (host) | Routed (CI, both runs) | Clearance violations (host) | Verdict |
|---|-------|---------------|------------------------|-----------------------------|---------|
| 0 | `U2` `rotate_180` --- from `DQ3`'s `DE_REVERSE_BUNDLE` verdict | 25 -> **16** | 25 -> **14** | 0 -> **1489** | reverted (reach collapse) |
| 1 | `U3` translate `(-0.77, -1.85)` mm --- from `MIPI_DAT0_N`'s `MOVE_PART` | 25 -> **26** | 25 -> **25** | 0 -> **2** | reverted (host: clearance; CI: no reach gain) |

The `Routed` columns are the loop's internal routed-net counter, whose
baseline at probe time is 25/31; the `26/31 -> 27/31` figures in the
prose below are the same host-measured +1 net expressed as final-artifact
reach (the probe-1 artifact measured 27/31 on that host).

Probe 0 is the informative negative result.  The classifier is **right**
that the DDR byte is reversed at `U2` --- it measures 28/28 facing pad
pairs inverted between `U1` and `U2` --- but a 180-degree rotation is not
the move that fixes it: it de-reverses the pad ORDER while relocating the
whole facing column to the *far* side of the package, so the byte then has
to wrap around a 7x7 mm QFN.  Reach collapses to 16/31.  The
geometrically correct move is a **mirror** of the pad column, which KiCad
expresses only as a layer flip and which the Phase-1 translator therefore
never emits.

Probe 1 is the real finding --- **on the local macOS arm64 host**.  There,
a single bounded 2 mm translate of `U3` **does** close `MIPI_DAT0_N` ---
26/31 -> 27/31 --- but the extra copper it lets into the DDR channel costs
manufacturability.  This is a host-specific measurement, not a universal
one: both independent CI runs (ubuntu-latest, same seed and
`PYTHONHASHSEED`) measured the same probe as routed **25 -> 25** --- no
reach gain --- so on CI the delta is refused for lack of a strict
routed-net increase and the clearance-regression guard never fires.  The
manufacturability cost below was measured on the host's 27/31 artifact:

* two segment-to-pad clearances drop under the jlcpcb floor
  (0.076 mm and 0.094 mm against a 0.102 mm minimum, at the `U1` DDR
  escape), taking blocking DRC from 10 to 13 on the same host; and
* the `ADDR_BUS` length-match tuner, which normally drives that group's
  skew to **0.000 mm**, is left at **11.03 mm** --- a
  `match_group_length_skew` error on the very board that exists to
  exercise that rule.

Shipping it would have required widening
`.github/routed-drc-tolerance.yml`'s board-07 floor from 8 to 13.  Trading
a DRC allowlist widening for one net is not an improvement on a
match-group/DRC testbench, so the loop's acceptance test now requires
*both* a reach gain and no clearance regression, and this delta is
refused.  The refusal is recorded, with its measurements, in
`output/matchgroup_test_routed_placement_delta.json` (the `reverted`
section carries `routed_before` / `routed_after` and `revert_reason`) so
the decision is auditable rather than invisible.

**The acceptance test is *relative*, and #4770 measured what that costs.**
`PlacementDeltaFeedbackLoop.run_delta` keeps a delta on
`new_count > pre_count` --- a comparison against the reach the loop
currently holds, not against an absolute floor.  So anything that
*depresses* the initial negotiated pass's reach also lowers the bar this
probe has to clear.  Issue #4770's A/B is the worked example: with the
relief-rescue sub-search bound switched from its 10 s wall clock to the
deterministic per-net expansion cap (#4730's withdrawn flip), the
negotiated pass completes 2 rip-up iterations instead of 5 and enters the
loop at 24/31 instead of 26/31 --- and *on CI* this same `U3` translate
then measures 23 -> 27, clears the gate, and is **kept**, taking blocking
DRC from 8 to 13 with exactly the 0.076 / 0.094 mm `U1`-escape clearances
and the 11.03 mm `ADDR_BUS` skew described above.  The `+1 net` in that
measurement is this delta, not a router improvement.  Full journal:
`diagnostic-runs/README.md`, section "Deterministic relief-rescue bound:
A/B measurement (#4770)".

Two useful side effects of running the loop, both measured on the same
host:

* the delta artifact is emitted on **every** run, so the classifier's
  per-net proposal for each open net is a build output rather than a
  one-off investigation; and
* blocking DRC on the shipped artifact measures **8** (equal to the
  committed allowlist floor) versus **10** for a loop-off local regen ---
  the loop's atomic revert now also rebuilds the routing grid, which the
  post-loop optimize/DRC-nudge stages read.

The shipping budget is **2**: probe 0 is the classifier's top-ranked rung
and probe 1 is the one that is only marginally short of acceptable, so if a
future router change removes those two clearances the loop will take the
net automatically.  A direct consequence: `DQ4`, `TMDS_D0_N` and
`TMDS_D1_N` are **unprobed-by-budget, not unroutable** --- the loop
proposes a delta for each of them (all proposals are recorded in the
delta artifact; CI logs `Deltas proposed: 10`), but the budget of 2 is
consumed by `DQ3`'s and `MIPI_DAT0_N`'s probes before those three are
ever tried.  Their `CONGESTION_SATURATED` / `PLACEMENT_BOUND` verdicts in
the table below describe why the *router* leaves them open at the current
placement; no placement delta for them has been probed and refused.  Each
budget unit costs one complete board-07 re-route; the whole route step
measures **27m05s** locally against ~16 min for the loop-off recipe.

#### Host-vs-CI divergence (probe-level only)

The probe-level routed counts above are host-dependent even at a fixed
seed.  Same code, `--seed 42`, `PYTHONHASHSEED=42`:

* local macOS arm64 host: probe 0 routed `25 -> 16`, probe 1 routed
  `25 -> 26` (reverted for clearance);
* CI ubuntu-latest: probe 0 routed `25 -> 14`, probe 1 routed `25 -> 25`
  (reverted for no reach gain) --- reproduced **bit-for-bit across two
  independent CI runs** on different head SHAs (PR #4556; run
  `30680605924` and its predecessor).

The *decisions* and the final state agree everywhere: 10 deltas proposed,
0 kept, exit `pd_reverted`, 26/31 reach, the identical 5-net open set,
and blocking DRC 8 at the committed floor.  Only the probe-level
narrative diverges.  This is the same honesty hazard as board-06's
run-to-run nondeterminism (#4536), on a cross-host axis: future
threshold or known-open decisions for this board should be made against
the CI numbers.

#### Mirror probe (#4560) --- first measurement

`DE_REVERSE_BUNDLE` now proposes `kind="mirror"` (a KiCad-semantics
layer flip of `U2` about its own anchor) instead of `rotate_180`, so
probe 0 of a fresh run is the mirror.  The structural diagnosis is
unchanged and still correct --- 28/28 facing pad pairs invert between
`U1` and `U2`, and a flip is the only orientation move that can
un-reverse a mirrored pin order (rotate_180 preserves chirality).

First measurement, **host-qualified per the divergence note above**
(macOS arm64, solo, `--seed 42`, `PYTHONHASHSEED=42`):

* probe 0 `U2` **mirror**: routed `25 -> 23`, clearance `0 -> 69`,
  reverted.  The flip moves `U2`'s pad column to `B.Cu`, which
  un-reverses the bundle *ordering* but relocates the entire DDR escape
  problem to the back side, where the routed baseline's copper and the
  skipped power fanout already live --- reach drops and the
  clearance-regression gate fires.  For calibration: the rotate_180 it
  replaces measured `25 -> 16` on this host (`25 -> 14` on CI), so the
  mirror is the better probe of the two, but neither survives.
* probe 1 `U3` translate: routed `25 -> 26`, clearance `0 -> 2`,
  reverted --- the same host-only marginal keep/refuse case documented
  above, refused again by the clearance gate.

Final state is bit-identical in shape to the committed baseline: 10
deltas proposed, 0 kept, exit `pd_reverted`, 26/31 reach, the identical
5-net open set (`DQ3`, `DQ4`, `MIPI_DAT0_N`, `TMDS_D0_N`, `TMDS_D1_N`).
The atomic revert (placement + router pads + routes + grid, now
layer-aware) restored the board exactly; the delta artifact records both
refusals with `routed_before`/`routed_after` and `revert_reason`.  CI
numbers for the mirror probe should be recorded from this change's
board-07 e2e job when available.

### Netlist write-back from a committed delta (#5537, Epic #5511 Phase 2b)

`regression-fixture/placement_delta.json` is a **committed, reviewed**
placement-delta artifact carrying the declared `DDR_BYTE0` swap group's
crossing-minimising pad re-binding for `U2` (`crossings_before: 28` ->
`crossings_after: 0`, measured by the #5522 proposer against
`matchgroup_test_routed.kicad_pcb` + `net_class_map.json`).  It is the
same `{pad_number: new_net_name}` payload
`kct net-status --why --format json` reports; the deltas were promoted
from the classifier's `proposed` list into the artifact's `applied`
section by that review, and **nothing replays `proposed` automatically**.

The recipe consumes it via `PlacementDelta.from_dict` and applies the
`pad_map` to **both** generated views *before* routing, so the swap
survives a full regeneration and the schematic/PCB still agree pad-for-pad
(a one-sided swap would surface as an LVS mismatch on every re-bound pad):

```sh
uv run python boards/07-matchgroup-test/generate_design.py /tmp/board07-swap \
  --placement-delta boards/07-matchgroup-test/regression-fixture/placement_delta.json
```

The run prints the change report (target, kind, source action, the
`crossings_before`/`crossings_after` evidence and every `pad -> net`
binding) and writes it to `placement_delta_applied.txt` next to the
regenerated artifacts.  `generate_schematic.py` and `generate_pcb.py`
accept the same flag for a single-view regen; `--step route` does not
(it consumes an already-generated PCB and says so).

**Opt-in only.** With the flag absent every generated byte is what it was
before #5537, so the committed witnesses in this directory are unaffected
and are **not** regenerated by that change: the historical open set, the
routed-DRC allowlist floor and the angle-census pin below all still
describe the board that is committed here.  Re-binding `U2`'s DQ pads
changes which nets the router must reach, which would invalidate every one
of those measurements, so promoting a swapped board to the committed
fixture is a separate, separately-measured decision.

The re-binding touches only the eight **declared** `DDR_BYTE0` members
(DQ0-DQ7 on `U2` pads 1-4 / 8-11).  `DM0`, `DQS_P` and `DQS_N` are
`DDR_DATA_BYTE_0` match-group siblings that are deliberately **not**
declared swappable and stay fixed, and no `ADDR_BUS` net is touched at
all.

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
2. Asserts every blocking DRC finding is listed **by identity** (rule +
   pair, net or match group, each with an open tracking issue) in
   `MATCHGROUP_FIXTURE_ALLOWED_FINDINGS` / `MATCHGROUP_ALLOWED_GROUPS` in
   `scripts/ci/check_matchgroup_coverage.py`, and that every listed entry
   still fires (a fixed finding fails the gate as stale until its entry
   is deleted).  The board has no entry in
   `.github/routed-drc-tolerance.yml`, so the count of blocking findings
   *outside* the allowance must be 0
   ([#7367](https://github.com/rjwalters/kicad-tools/issues/7367); the
   old count, ratcheted 35 -> 14 -> 9 -> 8, let a new finding hide
   behind a fixed one).  Advisory `connectivity` is not blocking; reach
   is gated by item 4.
   <!-- kct:doc-pin drc-tolerance boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb = 0 -->
   The claim above is machine-checked by `kct check --only doc_drift`
   (issue #4540): ratcheting the yml without updating this README
   trips `doc_drift_stale_pin`.
3. Asserts the `match_group_length_skew` DRC rule was actually
   exercised --- i.e., `rules_checked_by_rule["match_group_length_skew"] >= 1`
   in the `DRCChecker` summary.  Without this assertion a regression
   that disables match-group detection (e.g., a future change that
   breaks `derive_group_skew_data` or unwires `length_match_group`
   from the net classes) would silently produce a 0-violation report
   and hide the defect.
4. Asserts signal reach (Issue #7319): at least `REQUIRED_SIGNAL_REACH`
   (31 since #7557) of the 31 non-pour nets are complete per `NetStatusAnalyzer`,
   the same counter board 06's gate uses.  Below it the gate exits 2 and
   names the open nets.  The committed 27/31 routed artifact in this
   directory is below the contract on purpose; it is the witness that
   the assertion bites (`--skip-route` against it fails).

### Re-route reach (seed 42, Issue #7319)

The gate's seed-42 re-route reaches **31/31** since #7557 (see "U4 TMDS
ball move" below); before it, **30/31** (`TMDS_D0_N` open) with 0
placement-delta probes.  #7163 briefly dropped it to 29/31: the DDR strobe
pair carried no impedance target, so the router synthesized 100 ohm from
the validator's `.*_[PN]$` default and re-solved the DQS **width** at the
0.1016 mm manufacturer gap floor -- 0.175 mm under the #7163 field solve
(0.225 mm before it).  DQ0 went open and a `U2 mirror` probe tripled the
gate's wall time.  `DDR_DQS` now declares the 100 ohm target and is sized
like MIPI/HDMI (0.225 mm held, 0.135 mm gap, 99.7 ohm).  Measured
back-to-back on one host (C++ backend, `PYTHONHASHSEED=42`):

| run | iteration-0 | final | probes | channel phase | gate wall |
|---|---|---|---|---|---|
| `bd3c8fb10` (pre-#7163) | 30/31 | 30/31 | 0 | 507 s | 992 s |
| DQS sized (#7319) | 30/31 | 30/31 | 0 | 358 s | 702 s |

#### Blocking reason for `TMDS_D0_N` (Issue #7554, re-measured on 30/31)

Re-measured on the 30/31 layout (`main` @ `fd8a974cd`, C++ backend,
`PYTHONHASHSEED=42`, `generate_design.py <dir> --step all --seed 42`).
The negotiated router's access-loss witness names the cause:

```
TMDS_D0_N sealed in at iteration[1]: U4.B2 has no legal exit, closed by
TMDS_D1_P, TMDS_D2_P, U4.A2, U4.B1, U4.B3, U4.C2
```

Targeted rip-up (`TMDS_D0_P`, `TMDS_D1_P/N`, `TMDS_D2_P/N`) and two
relief-rescue rounds resolved 0/1: the victims cannot be re-landed.  The
pad is enclosed by neighbouring BGA pads plus the committed escapes of two
other TMDS legs, so the open is escape-bound (a 1:1 trade), not
budget-starved.  No router knob closes it; it needs a placement change or
joint TMDS escape-corridor allocation (#3673), tracked in #7557.  The run
was cut at a 600 s wall-clock cap, after the witness, so no final reach
figure was taken; the 30/31 figure above is unchanged.

#### U4 TMDS ball move (Issue #7557): 30/31 -> 31/31

U4 is a synthetic BGA-49 sink, so which ball carries which TMDS leg is a
free design choice.  The classifier offers no U4 delta for this open
(`classify_stuck_nets_from_pcb`: `CONGESTION_SATURATED`,
`co_oriented_bundle`, `move_part` / `accept_plateau` only, empty
`proposed`), so the candidates were measured directly.  Each was replayed
as a U4 `pad_map` through `generate_design.py <dir> --step all --seed 42
--placement-delta <file>` (C++ backend, `PYTHONHASHSEED=42`, `main` @
`b77661d2f`); the vacated row-B balls become `GND`:

| U4 TMDS balls (D0_P, D0_N, D1_P, D1_N, D2_P, D2_N) | reach | result |
|---|---|---|
| B1..B6 (baseline, row B, one ring in) | 30/31 | `TMDS_D0_N sealed in ... U4.B2 has no legal exit` |
| `{A2: TMDS_D0_N, B2: GND}` only (2026-10-20 builder note) | 30/31 | seal-in follows the net to `U4.A2` |
| A1..A6 (outer row A, top edge) | 30/31 | `U4.A2 has no legal exit, closed by TMDS_D1_P, TMDS_D2_P, U4.A1, U4.A3, U4.B2`; copper-LVS also opens `+1V2` (U4.C2) and `+1V8` (U4.D2) |
| **A1..F1 (outer column 1, J2-facing, top to bottom)** | **31/31** | copper-LVS clean, 0 unconnected; 748 segments, 28 vias, 983.52 mm |
| G1..B1 (outer column 1, bottom to top) | 31/31 | copper-LVS clean; 757 segments, 30 vias, 985.60 mm |

Column 1 faces J2 (which sits 25 mm to the left at the same y), so every
leg leaves the package straight toward the connector without threading
the row-A GND balls.  A1..F1 is committed (fewer vias, shorter) as a
recipe constant in `generate_pcb.py` / `generate_schematic.py` (both
generators, so the netlist stays consistent and CI needs no
`--placement-delta`), and the same 11-pad re-binding is applied to
`matchgroup_test.kicad_pcb` here so this gate's re-route sees it.  The
committed schematic and routed PCB in this directory stay the legacy
witness (the routed one is the 27/31 artifact item 4 relies on).

Before / after on the seed-42 re-route (pinned kicad-cli 10.0.6 via
`KCT_KICAD_DOCKER=1`; two consecutive `--step all` runs produced
byte-identical routed PCBs):

| | before (B1..B6) | after (A1..F1) |
|---|---|---|
| signal reach | 30/31 | 31/31 |
| copper-LVS | 1 open (`TMDS_D0_N`) | clean |
| native DRC (`--refill-zones`, errors) | 7 `skew_out_of_range` + 1 unconnected | 7 `skew_out_of_range` + 0 unconnected |
| `kct check` blocking (with sidecar) | 6 pair skew, 4 impedance, 2 group skew, 1 connectivity | 7 pair skew, 6 impedance, 3 group skew |

The new `kct check` findings are on the newly routed pair and the group
it completes, in rule families already tracked: `TMDS_D0` pair skew
(4.149 mm, P 2 vias / N 0, #7619), `TMDS_D0_P/N` impedance (125.2 ohm,
#7349) and `HDMI_TMDS_LANES` group skew (7.723 mm, #7550).  They are
listed by identity in `check_matchgroup_coverage.py`.  `TMDS_D1` pair
skew fell 4.968 -> 1.584 mm (no longer via-imbalanced, re-pointed from
#7619 to #7550); `TMDS_D2` moved 0.730 -> 0.833 mm.  No other net's findings changed.

The committed `net_class_map.json` here still describes the committed
routed artifact (DQS 0.150/0.100, MIPI/HDMI gap 0.100); it is regenerated
with that artifact under #6730 / #7283, not by the gate.

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
- **`Reach regression on re-routed ...`** -- fewer signal nets
  complete than `REQUIRED_SIGNAL_REACH` in `generate_design.py`.  The
  message names the open nets.  Measure which routing input changed
  (class geometry in the sidecar, per-net copper widths) before
  lowering the contract.
- **`Errors outside the identity allowance`** / **`DRC errors
  detected on re-routed ...`** -- the routing algorithm produced a
  finding on a rule/identity the allowance does not list.  Fix the
  router regression; an accepted, separately-tracked defect is listed
  by identity with its open tracker, never by count.
- **`Stale identity allowance`** -- a listed finding no longer fires:
  delete its entry.
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
