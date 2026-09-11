# Inspecting standard soldermask geometry

`kicad_tools.validate.mask_geometry.inspect_mask_geometry(path)` reads a PCB
and returns standard pad/via openings in board coordinates. This is a geometry
inspection API, **not a clearance check or manufacturing approval**. The broader
opening-to-copper checker is tracked in #5063; custom/export coverage is #5136
and exposure intent and process-specific thresholds are #5137.

```python
from kicad_tools.validate.mask_geometry import inspect_mask_geometry

snapshot = inspect_mask_geometry("board.kicad_pcb")
print(snapshot.to_dict())
if not snapshot.complete:
    print(snapshot.unsupported)  # never interpret partial geometry as clean DRC
```

The snapshot includes SHA256 hashes of the exact parsed PCB and any sibling
project/custom-rule files. Each opening carries its pad/via UUID, mask side,
geometry, owning copper geometry, effective expansion and expansion source.
Repeated inspection of unchanged inputs produces the same serialized output.
Geometries serialize as WKT; coordinates are KiCad board millimetres (y down).
Consumers must compare input hashes before reusing a saved result.

Supported geometry is circle, rectangle, oval and roundrect pads, including
board-frame pad rotation and footprint-local position transformation. Positive
expansion is a rounded offset, including on rectangular pads. Negative expansion
shrinks the opening and is identified as mask-defined; that label grants no waiver
and says nothing about whether a nearby conductor's exposure is intentional.
An opening can be empty when negative expansion closes it completely.

Explicit pad expansion (including zero) overrides footprint expansion (including
zero), then board setup expansion, then the native default zero. No manufacturer
margin is substituted. Front/back through-via tenting uses per-via overrides,
then board defaults; absent tenting defaults to tented under the tested native
KiCad 10 behavior. Untented via openings use board expansion. The API does not
claim to model arbitrary plot-setting overrides.

These semantics are checked against **KiCad 10.0.6** mask and copper Gerbers:
standard rectangle positive/zero/negative expansion, rotated roundrect corner
centres/radius, and tented/untented via apertures. The independent native tests
read exported aperture dimensions/macros rather than using this API to create
expected geometry. They skip explicitly if native KiCad is unavailable.

Circular geometry uses polygon approximation with at most 0.000001 mm chord
error per circular construction (an expanded rounded shape uses two constructions).
This is numerical approximation, not a fabrication tolerance or clearance floor.
Native comparison tests allow 0.000005 mm including Gerber decimal rounding.

Coverage is explicitly incomplete for custom/chamfered/advanced pads, mask-only
pads, mask graphics/text/zones, minimum-web merging, nonzero project mask settings,
custom rule files, unrecognized tenting, missing/duplicate UUIDs, and unsupported
via stacks. The supported subset is retained alongside diagnostics. Project-level
mask settings are deliberately reported unresolved: native probing shows legacy
project values do not simply override board setup, so the API does not guess at
that inheritance. Custom rule files are hashed and require native evaluation even
when they might not affect mask geometry. Malformed source or unreadable files
raise errors; they do not return a clean empty result.

The [KiCad PCB editor documentation](https://docs.kicad.org/9.0/en/pcbnew/pcbnew.html)
describes mask expansion and footprint/pad overrides; the fixture tests establish
the concrete native version used here. No generic JLCDFM value is asserted to be
a JLCPCB ordering limit, and no supplier qualification has been performed.
