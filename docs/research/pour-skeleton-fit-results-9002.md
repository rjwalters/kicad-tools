# Pour-skeleton fit fleet measurement (null result)

**Issue**: #9002 (smallest next step of the [WeaveEngine evaluation](weaveengine-topology-first-evaluation.md), idea 5 "pour-integrity reservation during routing")
**Date**: 2026-11-17
**Provenance**: ideas-only (WeaveEngine is GPL-3.0, we are MIT) — the predictor is implemented from the behaviour description in the issue, never from upstream code.
**Tool**: `scripts/pour_skeleton_fit.py` (new; geometry only, plus an optional kicad-cli refill-gate mode). Ground truth: `kicad-cli pcb drc --refill-zones` on the pinned evidence toolchain (10.0.6, scratch `KICAD_CONFIG_HOME`, refusal on `lib_footprint_issues`), tree `89c2ee058`.

## TL;DR

**Null result.** After refilling every routed fleet benchmark board, kicad-cli reports **zero `isolated_copper` findings and zero `unconnected_items` on all nine boards** (00–07, 09). The routed pours are never cut into pad-less islands, so there is no baseline of real pour-cutting to justify router-reservation work. Per the issue's decision rule ("thin tail → record the null result and close") no router-reservation follow-up is filed and this issue closes as measured-and-not-needed.

The cheap predictor itself (straight pad-joining corridors at the zone's own `min_thickness`/`clearance`) over-predicts: 6 board×net predictions, all false. Every false positive is a pour sharing a layer with signal copper (boards 01/03/04/05), where the refill weaves bent corridors a straight-line model cannot represent. Pours on dedicated inner layers — where the auto-pour allocator puts ground planes on 4+-layer stackups — are predicted FIT and are clean.

## Which board is "the dense #8610 mains board"?

Confirmed from #8610: it is a **private** dense 4-layer mains board (the ~282 Net-0 shorts); its body states the board cannot be checked in. The in-repo candidate `boards/synthetic-mains-complete/` is **not pour-relevant**: it is the 2-layer creepage fixture of #8422, carries **no zones at all** (its recipe passes `--no-auto-pour`), and its footprint library nickname makes the pinned evidence toolchain refuse it (`lib_footprint_issues`) — correctly, per the committed-evidence contract. The measurement therefore uses the fleet's own dense boards (03/05 four-layer, 06/07/09 six-layer) as the nearest in-repo population; board 07 (8,771 segments, 6 layers) is the densest.

## Model

Per pour zone, per copper layer (`scripts/pour_skeleton_fit.py` docstring has the full contract):

- **Anchors** = the zone net's pads on that layer (excluding `(zone_connect 0)` pads) plus *live* same-net vias (cluster reaches a pad somewhere). Zero anchors → unfit (every island of such a fill is isolated copper by definition — the `orphan_island` semantics).
- Anchors already in one transitive same-net copper cluster (touching pads/tracks/vias; committed fills deliberately excluded — the fill is the thing being predicted) need no corridor.
- **Skeleton edges** = straight corridors; an edge fits iff `LineString(a,b).buffer(min_thickness/2)` lies inside the zone outline minus foreign copper (pads, vias, segments via `fill_clearance._collect_obstacles`) grown by the zone clearance, minus copper keepouts. Foreign zone fills are not blockers (the refill negotiates those itself).
- Net **predicted-unfit** iff some zone-layer's anchors split into ≥2 groups no fitting corridor joins (or the zone-layer has no anchors).

Positive control: on `tests/fixtures/drc/orphan_island.kicad_pcb` (no GND anchor anywhere) the script predicts GND unfit and the refill gate reports 1 `isolated_copper` finding for GND — precision = recall = 1.00 on the one true-positive case in the repo.

## Results

Prediction and refill gate, one routed board per fleet entry (kicad-cli 10.0.6, 2026-11-17):

| Board | Routed PCB | Pour zone-layers (net / layer / anchors → verdict) | Predicted-unfit nets | Actual `isolated_copper` | `unconnected_items` |
|---|---|---|---|---|---|
| 00 simple LED | `output/simple_led_routed.kicad_pcb` | VCC F.Cu 2 → FIT · GND B.Cu 2 → FIT | — | 0 | 0 |
| 01 voltage divider | `output/voltage_divider_routed.kicad_pcb` | GND F.Cu 3 → FIT · GND B.Cu 2 → **UNFIT** | GND | 0 | 0 |
| 02 charlieplex | `output/charlieplex_3x3_routed.kicad_pcb` | *(no zones)* | — | 0 | 0 |
| 03 USB joystick | `output/usb_joystick_routed.kicad_pcb` | GND F.Cu 47 → **UNFIT** (21 groups) · GND B.Cu 11 → **UNFIT** (3) · GND In1.Cu 11 → **UNFIT** (2) · VCC In2.Cu 19 → FIT | GND | 0 | 0 |
| 04 STM32 devboard | `output/stm32_devboard_routed.kicad_pcb` | +3.3V F.Cu 15 → **UNFIT** (5) · +5V F.Cu 2 → FIT · GND B.Cu 17 → **UNFIT** (2) | +3.3V, GND | 0 | 0 |
| 05 BLDC controller | `output/bldc_controller_routed.kicad_pcb` | GND F.Cu 89 → **UNFIT** (3) · GND B.Cu 63 → **UNFIT** (8) · GND In1.Cu 61 → **UNFIT** (4) · +5V In2.Cu 26 → **UNFIT** (2) | +5V, GND | 0 | 0 |
| 06 diffpair test | `output/diffpair_test_routed.kicad_pcb` | GND In1.Cu 38 → FIT · +3V3 In2.Cu 26 → FIT | — | 0 | 0 |
| 07 SDRAM exerciser (densest) | `output/matchgroup_test_routed.kicad_pcb` | GND In1.Cu 52 → FIT · +3V3 In4.Cu 28 → FIT | — | 0 | 0 |
| 09 USB-C PD | `output/usbc_pd_power.kicad_pcb` | GND In1.Cu 27 → FIT · GND In2.Cu 27 → FIT | — | 0 | 0 |

Aggregate at net level: 6 predicted-unfit (board, net) pairs, 0 actual unfit nets anywhere → **precision 0/6, recall undefined (no positives exist to find)**. Every zone on every board uses `island_removal_mode 0` (never remove), so a pad-less island would have been kept *and* reported — the zeros are real absences, not islands silently discarded by the filler.

Representative witnesses (all false positives — the refill routes around what a straight corridor cannot):

- board 01, GND B.Cu: `J2.2 (158.5,88.8) → J1.2 (138.5,88.8): blocked by segment net 'VOUT'` — the VOUT escape crosses the B.Cu pour, both segment ends stop short of the outline, and the refill detours around an end.
- board 04, +3.3V F.Cu: `U2.1 → U2.9: blocked by pad net 'OSC_IN'` — straight corridors through the MCU pad field never fit; the pour weaves between pads.

## Interpretation

- **The gap the issue hypothesised is empty on our fleet.** `auto_pour`'s layer allocator already places ground pours on dedicated inner layers of 4+-layer stackups, where nothing can cut them, and on the shared-layer boards the refill's bent corridors find ways the straight-corridor model cannot. Zero `isolated_copper`, zero `unconnected_items`, nine boards.
- **The cheap predictor is not shippable as a warning either**: 6/6 false positives means a route-time warning would be pure noise on this fleet. Making it useful would need per-edge bent-corridor search (exactly the cost the WeaveEngine history warns about — their settled design dropped skeleton negotiation *with* traces as too slow and connectivity-fragile), and there is nothing true for it to find.
- **Per the issue's decision rule, this closes the idea rather than filing the router-reservation follow-up.** If a future board (e.g. a checked-in descendant of the #8610 mains board) shows real refill islands, re-run the script first — the measurement takes ~55 s including the refill gates.

## Reproducing

```bash
uv run python scripts/pour_skeleton_fit.py boards/*/output/*_routed.kicad_pcb \
    boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb --refill-gate --json /tmp/fleet.json
```

(The glob over-approximates; use the exact board list in the table for byte-for-byte parity. `--refill-gate` refuses to run on kicad-cli ≠ 10.0.6 or on a `lib_footprint_issues` report, per the committed-evidence contract.)
