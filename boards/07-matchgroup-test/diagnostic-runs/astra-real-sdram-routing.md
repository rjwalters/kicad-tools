# Astra real SDRAM redesign: routing investigation

2026-09-09–10. The assembled replacement is staged under `real_design/`; the
published synthetic fixture remains intact until the replacement passes all
gates. The real circuit is STM32F429ZIT6 + IS42S16400J-6TLI-TR, with independent
power, SWD, UART and memory-test firmware. No test below waives native DRC,
logical connectivity, timing or impedance requirements.

| Finding | Reproduction and result |
|---|---|
| Explicit stackup ignored during lazy impedance sizing ([#5002](https://github.com/rjwalters/kicad-tools/issues/5002)) | DQ1 changed from authored 0.160 mm to 0.375 mm at `_prepare_routing()`, using a generic 0.2104 mm outer dielectric instead of the explicit 0.0994 mm stack. Isolated routing failed in 0.184 s. Preferring the actual source stack produced quantized 0.150 mm, within 50 Ω±10%, and the same route succeeded in 0.557 s. |
| Fine-pitch pad-clearance exemptions ([#5004](https://github.com/rjwalters/kicad-tools/issues/5004)) | The legacy router reported 0 pad-clearance violations while fresh native KiCad found 46: 30 against NC pins and 16 against named pins. Actual gaps were 0.100–0.143 mm against the authored 0.150 mm floor. `--strict-pad-clearance` now retains these checks in both backends; default legacy behavior remains unchanged. |
| Seed points on pad metal edges ignored trace radius ([#5005](https://github.com/rjwalters/kicad-tools/issues/5005)) | DQ1's reconstructed tail left center (144.1625, 107.25) toward y 107.10, leaving only 0.125 mm to its neighbor. All five native resumes repeated this illegal tail. Insetting strict-mode seed/goal regions by half trace width produced a valid isolated route in 0.601 s. |
| Bottom microstrip looked outward through mask/paste ([#4997](https://github.com/rjwalters/kicad-tools/issues/4997)) | Physics now uses the inward substrate below bottom copper, with asymmetric-stack and mirrored-face regression coverage. |
| Four-line rectangular outline loaded as HAT fallback ([#4978](https://github.com/rjwalters/kicad-tools/issues/4978)) | Generator emits the same physical rectangle as `gr_rect`; router now loads 100×80 mm at (98.5, 65), not 65×56 mm. |

The strict run's raw routing checkpoint was independently checked with the
original project: **0 native violations, 130 unconnected items**. This is a
clearance improvement, not a complete board. Its saved native report is
`/tmp/board07-sdram/strict-initial-native/native-drc.json`; the immutable audited
copy is `check.kicad_pcb` beside it. The earlier 46-error report is
`/tmp/board07-sdram/authored-stack-checkpoint-validation/native-drc.json`.

A placement alternative rotates U2 by 180° and derives its decoupler positions
from actual rotated supply pads. Its unrouted source passes native placement
DRC with 0 violations / 157 unconnected items. An early rotated run with 0.20mm
routing guardband plateaued, so no placement improvement is claimed yet.
Generator flags keep this experiment reproducible without changing the
original comparison: `--sdram-rotation 180 --routing-clearance .20`.

The strict build requires the actual-stackup fix and `--strict-pad-clearance`.
The current full campaign raises the search budget from 200k to 1M node
expansions per net, with three negotiation iterations and 600 s overall budget.
Only computational limits changed; native clearance remains 0.15 mm and bus
budgets remain 5 mm within groups, 10 mm clock-relative, 120 mm maximum and 50 Ω±10%.
Partial checkpoints and stopped runs are not manufacturing outputs.


## Via-terminal experiment and layer-contract audit

The four-layer via-terminal experiment added 127 real 0.6/0.3 mm through vias
with alternating inward/outward 1.25 mm normal stubs at U1/U2. The source had
zero native errors. Routing-only terminal substitutions retained the original
pad copper as obstacles; exact original footprints were restored before any
physical validation. An independent geometry comparison rejects any leaked
proxy footprint, even if its logical pin/net mapping still matches.

A saved checkpoint had 34 fully connected nets, zero native clearance errors,
and 116 native unconnected items. **It is rejected as a production candidate:**
8,842 signal segments occupied intended reference-plane layers, and the router
emitted partial-depth ordinary vias. Native clearance alone did not detect the
intended manufacturing/stackup mismatch. The source-specific impedance and
reference-layer gate did detect it. An interrupted raw save additionally lost
preserved fanout; it is not used as a continuation base.

- [#5013](https://github.com/rjwalters/kicad-tools/issues/5013) tracks the router's
  logical-transition versus physical-through-via span mismatch (both backends).
- [#5014](https://github.com/rjwalters/kicad-tools/issues/5014) tracks the missing
  reference-plane reservation guardrail. `LayerType.PLANE` is metadata;
  `allowed_layers`, or complete `avoid_layers` plus `--strict-layers`, is needed.
  Board06 also reproduced this with ordinary SMD endpoints.

Evidence: `/tmp/board07-sdram/via-ports/physical-checkpoint.kicad_pcb`,
`checkpoint-native.json`, and `incremental-probe.log` in the parent scratch
folder. The rejected checkpoint is retained only for reproduction.

A six-layer construction is now being evaluated using the factory's
[JLC06161H-2116A stack](https://jlcpcb.com/impedance): 0.1164 mm outer prepregs,
0.13 mm adjacent inner cores, and a central 0.1164/0.7/0.1164 mm laminate.
F.Cu/In2.Cu/In3.Cu/B.Cu are explicitly allowed signal layers; In1.Cu and In4.Cu
are reserved GND and +3V3 references. Source native checks remain zero errors.
Ordinary vias are expanded to their full physical F.Cu/B.Cu span before route
acceptance in both backends. The construction and asymmetric inner impedance
still require independent validation; it is not a promoted board or order
package. Scratch source and logs: `/tmp/board07-sdram/sixlayer/`.
