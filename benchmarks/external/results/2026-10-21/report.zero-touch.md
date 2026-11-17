# External autorouter benchmark results (zero-touch)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| pocketbeagle | zero-touch | 0.0% | 0 of 296 | 0 | 0.00 | 4.6 s (failed) | cpp | FAIL (394E / 987W) | 0 | n/a | failed | router output |
| beagleconnect_freedom | zero-touch | 28.5% | 131 of 460 | 0 | 0.00 | 42.3 s (partial) | cpp | not run | 426 | 0/4 | partial | router output |
| strf | zero-touch | 44.9% | 44 of 98 | 8 | 147.45 | 1811.6 s (partial) | cpp | FAIL (317E / 154W) | 148 | 2/2 | partial | router output |

**Placement disposition for this attempt**

- `beagleconnect_freedom` (zero-touch): placement_invalid; 157 requested, 156 eligible, 26 completed, 1 requested placement-blocked. Physical connectivity above is measured separately.
  Invalid footprint references: `SH1`.
  Direct: placement-invalid, not attempted: `GND`.
  Plane/pour exclusions: `3V3`, `5V`, `VDD`, `VDDS`.
- `strf` (zero-touch): valid; 46 requested, 46 eligible, 43 completed, 0 requested placement-blocked. Physical connectivity above is measured separately.
  Plane/pour exclusions: `+3V3`, `+5V`, `GND`.

**Notes**

- `pocketbeagle` (zero-touch): seed=42
- `pocketbeagle` (zero-touch): router raised ValueError: Layer 'In1.Cu' is not available on this board. Declared copper layers: .
- `pocketbeagle` (zero-touch): human baseline (pre-rip-up): 1297 segments, 171 vias, 2561.0mm, 78 unrouted pads
- `pocketbeagle` (zero-touch): DeepPCB published reference: {'airwires': 290}
- `beagleconnect_freedom` (zero-touch): seed=42
- `beagleconnect_freedom` (zero-touch): router exit code: 1
- `beagleconnect_freedom` (zero-touch): human baseline (pre-rip-up): 2291 segments, 399 vias, 2791.2mm, 25 unrouted pads
- `beagleconnect_freedom` (zero-touch): DeepPCB published reference: {'airwires': 414}
- `strf` (zero-touch): seed=42
- `strf` (zero-touch): router exit code: 4
- `strf` (zero-touch): human baseline (pre-rip-up): 346 segments, 70 vias, 287.8mm, 17 unrouted pads
- `strf` (zero-touch): DeepPCB published reference: {'airwires': 98, 'completion_pct': 100, 'vias': 68, 'wall_clock_minutes_max': 3}

**Reproduction**

- `pocketbeagle`: https://github.com/beagleboard/pocketbeagle @ `d793a63f48dd3041e333362d5ec870377d255f89` (kicad-tools `a802bab`, protocol `zero-touch`)
- `beagleconnect_freedom`: https://git.beagleboard.org/beagleconnect/freedom @ `3f99c08de7d81991f95eb2c23f30798a129ffdcf` (kicad-tools `a802bab`, protocol `zero-touch`)
- `strf`: https://github.com/pms67/STRF-Kicad @ `0525ef655e460ff6d91d770582b47925e7852e7a` (kicad-tools `a802bab`, protocol `zero-touch`)
