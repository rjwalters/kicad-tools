# External autorouter benchmark results (zero-touch)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| strf | zero-touch | 21.4% | 21 of 98 | 0 | 5.23 | 137.0 s (partial) | cpp | FAIL (43E / 121W) | 146 | 0/2 | partial | router output |

**Notes**

- `strf` (zero-touch): seed=42
- `strf` (zero-touch): router exit code: 2
- `strf` (zero-touch): human baseline (pre-rip-up): 346 segments, 70 vias, 287.8mm, 17 unrouted pads
- `strf` (zero-touch): DeepPCB published reference: {'airwires': 98, 'completion_pct': 100, 'vias': 68, 'wall_clock_minutes_max': 3}

**Reproduction**

- `strf`: https://github.com/pms67/STRF-Kicad @ `0525ef655e460ff6d91d770582b47925e7852e7a` (kicad-tools `696315f`, protocol `zero-touch`)
