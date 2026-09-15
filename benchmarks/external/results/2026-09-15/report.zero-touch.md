# External autorouter benchmark results (zero-touch)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| beagleconnect_freedom | zero-touch | 42.8% | 197 of 460 | 1 | 193.06 | 2175.9 s (partial) | cpp | not run | 383 | 0/4 | partial | router output |

**Placement disposition for this attempt**

- `beagleconnect_freedom` (zero-touch): placement_invalid; 157 requested, 156 eligible, 54 completed, 1 requested placement-blocked. Physical connectivity above is measured separately.
  Invalid footprint references: `SH1`.
  Direct: placement-invalid, not attempted: `GND`.
  Plane/pour exclusions: `3V3`, `5V`, `VDD`, `VDDS`.

**Notes**

- `beagleconnect_freedom` (zero-touch): seed=42
- `beagleconnect_freedom` (zero-touch): router exit code: 2
- `beagleconnect_freedom` (zero-touch): human baseline (pre-rip-up): 2291 segments, 399 vias, 2791.2mm, 25 unrouted pads
- `beagleconnect_freedom` (zero-touch): DeepPCB published reference: {'airwires': 414}

**Reproduction**

- `beagleconnect_freedom`: https://git.beagleboard.org/beagleconnect/freedom @ `3f99c08de7d81991f95eb2c23f30798a129ffdcf` (kicad-tools `0e8a9a9d`, protocol `zero-touch`)
