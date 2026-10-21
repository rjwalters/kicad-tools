# External autorouter benchmark results (tuned)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| strf | tuned | 44.9% | 44 of 98 | 6 | 142.13 | 1881.9 s (partial) | cpp | FAIL (321E / 154W) | 152 | 1/2 | partial | router output |

**Placement disposition for this attempt**

- `strf` (tuned): valid; 46 requested, 46 eligible, 43 completed, 0 requested placement-blocked. Physical connectivity above is measured separately.
  Plane/pour exclusions: `+3V3`, `+5V`, `GND`.

**Notes**

- `strf` (tuned): seed=42
- `strf` (tuned): router exit code: 4
- `strf` (tuned): human baseline (pre-rip-up): 346 segments, 70 vias, 287.8mm, 17 unrouted pads
- `strf` (tuned): DeepPCB published reference: {'airwires': 98, 'completion_pct': 100, 'vias': 68, 'wall_clock_minutes_max': 3}
- `strf` (tuned): tuned protocol: applied declared net-class-map (bench-out/tuned/tuned/strf.net_class_map.json) via --net-class-map --differential-pairs -- see benchmarks/external/tuned_rules.py for the source values and schema-mapping caveats

**Reproduction**

- `strf`: https://github.com/pms67/STRF-Kicad @ `0525ef655e460ff6d91d770582b47925e7852e7a` (kicad-tools `a802bab`, protocol `tuned`)
