# External autorouter benchmark results (zero-touch)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| pocketbeagle | zero-touch | 31.8% | 94 of 296 | 0 | 0.00 | 1.7 s (failed) | cpp | FAIL (394E / 801W) | 0 | n/a | failed | fallback input |

**Measured artifact is fallback input, not router output**

- `pocketbeagle` (zero-touch): the router exited 1 (non-zero) and produced no output file for this attempt -- no routing progress was captured; measuring the pre-route input as fallback -- connectivity, copper and check measurements describe the pre-route input. Runtime measures the routing attempt.

**Notes**

- `pocketbeagle` (zero-touch): seed=42
- `pocketbeagle` (zero-touch): router exit code: 1
- `pocketbeagle` (zero-touch): human baseline (pre-rip-up): 1297 segments, 171 vias, 2561.0mm, 78 unrouted pads
- `pocketbeagle` (zero-touch): router produced no output file for this attempt -- measuring the pre-route (ripped-up) input as fallback; the completion% below reflects pre-existing/trivial connectivity, NOT new routing progress (see route_outcome / pre_route_completion in the JSON report)
- `pocketbeagle` (zero-touch): DeepPCB published reference: {'airwires': 290}

**Reproduction**

- `pocketbeagle`: https://github.com/beagleboard/pocketbeagle @ `d793a63f48dd3041e333362d5ec870377d255f89` (kicad-tools `696315f`, protocol `zero-touch`)
