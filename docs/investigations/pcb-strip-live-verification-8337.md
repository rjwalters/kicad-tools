# Live verification of `kct pcb strip --from-report` / `--dangling` (issue #8337)

Measured on `main` @ `8b944ab44`, 2026-11-04, kicad-cli 10.0.6 from
`KCT_KICAD_DOCKER=1 uv run python -m kicad_tools.cli.kicad_toolchain --check`
(pinned image `kicad/kicad@sha256:18693567...dd3ec`, `Evidence toolchain: ok`).
Board: `boards/05-bldc-motor-controller/regression-fixture/bldc_controller_routed.kicad_pcb`
(11 `clearance` errors). Scratch copy only; no committed board was modified.
The DRC rows below come from `kicad-cli pcb drc --refill-zones --format json`,
run through the pinned-image kicad-cli shim that the `kicad_toolchain --check`
evidence wrapper provides (not a bare host `kicad-cli`). The board's only
footprint-library finding is `lib_footprint_mismatch`, so the #6962
`lib_footprint_issues` refusal was not hit.

## Outcome: two modes verified working, one measured refutation, two defects filed

| Check | Result |
|---|---|
| `--dangling` on the original board | PASS. 6 segments + 1 via removed in 4 passes; `track_dangling` 3 -> 0, `annular_width` 11 -> 10, nothing else changed; gate said `verified with kicad-cli pcb drc --refill-zones`; file written. |
| `--from-report` cut itself | PASS. 11 segments cut across 5 nets; `clearance` 11 -> 0 (independent DRC of the cut board). |
| `--from-report` write gate | REFUTED as a usable workflow: refuses to write because `track_dangling` warnings rose 3 -> 6. `--unverified` does not override. Filed #8347. |
| `kct creepage` witness vs `_creepage_findings` | MISMATCH: real witness is `{point_a_mm, point_b_mm, item_a, item_b}`; parser reads `point`/`position`/`x,y`/`uuid`. 114 rows, 0 located, 0 removed. Filed #8346. |

## DRC counts (type: original / cut / original+dangling / cut+dangling)

| type | orig | cut | orig+dang | cut+dang |
|---|---|---|---|---|
| clearance | 11 | 0 | 11 | 0 |
| track_dangling | 3 | 6 | 0 | 2 |
| unconnected | 57 | 61 | 57 | 61 |
| annular_width | 11 | 11 | 10 | 10 |
| track_width / silk_* / lib_footprint_mismatch (55) | unchanged | unchanged | unchanged | unchanged |

The "cut" board was produced by applying `plan_from_report` through the Python
API and `PCB.save`, because the CLI refused to write it (see above).

## Commands

```bash
export KCT_KICAD_DOCKER=1
# `kicad-cli` below = the pinned-image shim from the evidence toolchain, on PATH
kicad-cli pcb drc --refill-zones --format json -o drc.json bldc.kicad_pcb
kct pcb strip bldc.kicad_pcb --from-report drc.json -o cut.kicad_pcb
#   -> "Refusing to write: kicad-cli --refill-zones DRC reports NEW violations" (rc=1)
kct pcb strip bldc.kicad_pcb --dangling -o bldc-dang.kicad_pcb
#   -> Removed: 6 segment(s), 1 via(s); Passes: 4; DRC: verified; Saved
kct creepage bldc.kicad_pcb --standard iec60664 --pollution-degree 2 \
  --voltage-map vmap.json --census-threshold 10 --witness --format json > creepage.json
kct pcb strip bldc.kicad_pcb --from-report creepage.json --dry-run
#   -> every row "skipped: no location"
```

`vmap.json` is `{"+24V": 230, "VIN": 230, "GND": 0}`: synthetic potentials used
only to make a low-voltage board produce pairs; the creepage pass/fail values are
not meaningful.

## Not covered

- The host `kicad-cli` fallback (non-Docker) was not compared.
- Only one board was used; the pinned-toolchain check covers the kicad-cli
  release, and the library tables used by the `kct` code path were not
  separately inspected here.
