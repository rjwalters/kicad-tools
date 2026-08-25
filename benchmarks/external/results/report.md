# External autorouter benchmark results

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs |
|---|---|---|---|---|---|---|---|---|---|---|
| pocketbeagle | zero-touch | 31.8% | 94 of 296 | 0 | 0.00 | 2.8 s | cpp | FAIL (394E / 801W) | 0 | n/a |
| beagleconnect_freedom | zero-touch | 32.6% | 150 of 460 | 0 | 0.00 | 4.4 s | cpp | FAIL (8337E / 590W) | 417 | 0/4 |

**Notes**

- `pocketbeagle` (zero-touch): seed=42
- `pocketbeagle` (zero-touch): router exit code: 1
- `pocketbeagle` (zero-touch): human baseline (pre-rip-up): 1297 segments, 171 vias, 2561.0mm, 78 unrouted pads
- `pocketbeagle` (zero-touch): router produced no output file -- reporting the unrouted, ripped-up board (0% complete) rather than a stale artifact
- `pocketbeagle` (zero-touch): DeepPCB published reference: {'airwires': 290}
- `beagleconnect_freedom` (zero-touch): seed=42
- `beagleconnect_freedom` (zero-touch): router exit code: 2
- `beagleconnect_freedom` (zero-touch): human baseline (pre-rip-up): 2291 segments, 399 vias, 2791.2mm, 25 unrouted pads
- `beagleconnect_freedom` (zero-touch): router produced no output file -- reporting the unrouted, ripped-up board (0% complete) rather than a stale artifact
- `beagleconnect_freedom` (zero-touch): DeepPCB published reference: {'airwires': 414}

**Reproduction**

- `pocketbeagle`: https://github.com/beagleboard/pocketbeagle @ `d793a63f48dd3041e333362d5ec870377d255f89` (kicad-tools `636fd368`, protocol `zero-touch`)
- `beagleconnect_freedom`: https://git.beagleboard.org/beagleconnect/freedom @ `3f99c08de7d81991f95eb2c23f30798a129ffdcf` (kicad-tools `636fd368`, protocol `zero-touch`)
