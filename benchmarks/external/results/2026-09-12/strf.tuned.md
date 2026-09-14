# External autorouter benchmark results (tuned)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| strf | tuned | 23.5% | 23 of 98 | 0 | 70.25 | 1260.1 s (partial) | cpp | FAIL (95E / 8W) | 144 | 0/2 | partial | router output |

**Notes**

- `strf` (tuned): seed=42
- `strf` (tuned): router exit code: 1
- `strf` (tuned): human baseline (pre-rip-up): 346 segments, 70 vias, 287.8mm, 17 unrouted pads
- `strf` (tuned): DeepPCB published reference: {'airwires': 98, 'completion_pct': 100, 'vias': 68, 'wall_clock_minutes_max': 3}
- `strf` (tuned): tuned protocol: applied declared net-class-map (/evidence/cases/strf.tuned/output/tuned/strf.net_class_map.json) via --net-class-map --differential-pairs -- see benchmarks/external/tuned_rules.py for the source values and schema-mapping caveats

**Reproduction**

- `strf`: https://github.com/pms67/STRF-Kicad @ `0525ef655e460ff6d91d770582b47925e7852e7a` (kicad-tools `696315f`, protocol `tuned`)
