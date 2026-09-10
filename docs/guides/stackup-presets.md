# Named JLCPCB stackups and legacy migration

Select the physical factory construction explicitly when calculating controlled
impedance or preparing an order. The following 1.6 mm constructions were checked
against [JLCPCB's primary impedance table](https://jlcpcb.com/impedance) on
2026-09-10 (1 oz outer / 0.5 oz inner ordering options):

| Identifier | Outer prepreg, each | Prepreg εr | Inner copper, each | Core | Core εr |
|---|---:|---:|---:|---:|---:|
| JLC04161H-3313 | 0.0994 mm | 4.1 | 0.0152 mm | 1.265 mm | 4.6 |
| JLC04161H-7628 | 0.2104 mm | 4.4 | 0.0152 mm | 1.065 mm | 4.6 |

Both use 0.035 mm outer copper. Nominal 1.6 mm ordering thickness is not the sum
of these layer entries. Surface finish is not prescribed. The retained loss
tangent 0.02 is a calculation assumption, not a factory specification.

```python
from kicad_tools.physics import Stackup
stack = Stackup.jlcpcb_named("JLC04161H-3313")
```

```sh
kct impedance stackup --preset jlcpcb-3313 --format json
kct impedance width --preset jlcpcb-7628 --target 50 --layer F.Cu --format json
```

JSON summaries/calculations identify construction and provenance. An explicit
PCB input remains authoritative over a preset argument; parsing a board never
replaces its authored layers or infers a factory order identifier.

## Compatibility

`Stackup.jlcpcb_4layer()` and CLI `jlcpcb-4` / `generic-4` retain their historical
numbers: 0.2104 mm prepreg, εr 4.05, 0.0175 mm inner copper and 1.065 mm core.
The explicit compatibility entry points are `Stackup.jlcpcb_4layer_legacy()` and
`--preset jlcpcb-4-legacy`. This model has no factory ordering identifier; earlier
3313/7075 labels were incorrect. Default router/validator fallback and impedance
activation/dormancy remain unchanged. Migration is opt-in: select a named model,
review widths/spacing against the intended construction, and update the board
explicitly through the normal design review process. No boards are regenerated.

## Manufacturing ordering record

```sh
kct export board.kicad_pcb --mfr jlcpcb --stackup-id JLC04161H-3313
```

The exporter validates the selected construction against the actual PCB's
explicit ordered copper/dielectric layers and nominal thickness before writing
output. Missing explicit data, wrong layer/thickness/permittivity or a different
manufacturer fails even if normal preflight is skipped. It does not synthesize
or replace board geometry. Comparison uses 1e-6 absolute numerical tolerance.

Successful selected exports include `stackup-ordering.json` in the manifest,
with the source PCB SHA-256, actual layers, matching factory identifier and dated
source provenance. It is checked again after generation. Soldermask, surface
finish, loss tangent and manufacturing tolerances require separate review; this
record proves the modeled construction match, not factory acceptance or hardware
qualification. Exports without an explicit selection make no new factory claim.
