# External autorouter benchmark results (zero-touch)

| Board | Protocol | Completion | Connections | Vias | Wirelength (mm) | Runtime | Backend | kct check | kicad-cli DRC | Diff pairs | Outcome | Artifact |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| beagleconnect_freedom | zero-touch | 30.2% | 139 of 460 | 0 | 0.00 | 89.3 s (failed) | cpp | not run | 429 | 0/4 | failed | fallback input |

**Measured artifact is fallback input, not router output**

- `beagleconnect_freedom` (zero-touch): the router exited 2 (non-zero) and produced no output file for this attempt -- no routing progress was captured; measuring the pre-route input as fallback -- connectivity, copper and check measurements describe the pre-route input. Runtime measures the routing attempt.

**Notes**

- `beagleconnect_freedom` (zero-touch): seed=42
- `beagleconnect_freedom` (zero-touch): router exit code: 2
- `beagleconnect_freedom` (zero-touch): human baseline (pre-rip-up): 2291 segments, 399 vias, 2791.2mm, 25 unrouted pads
- `beagleconnect_freedom` (zero-touch): router produced no output file for this attempt -- measuring the pre-route (ripped-up) input as fallback; the completion% below reflects pre-existing/trivial connectivity, NOT new routing progress (see route_outcome / pre_route_completion in the JSON report)
- `beagleconnect_freedom` (zero-touch): DeepPCB published reference: {'airwires': 414}

**Reproduction**

- `beagleconnect_freedom`: https://git.beagleboard.org/beagleconnect/freedom @ `3f99c08de7d81991f95eb2c23f30798a129ffdcf` (kicad-tools `696315f`, protocol `zero-touch`)
