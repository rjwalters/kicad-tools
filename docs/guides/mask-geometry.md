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

Present malformed numeric or tenting fields return incomplete coverage with no openings before tolerant schema recovery can substitute defaults. This includes footprint/pad/via positions and rotations, dimensions, mask expansion, roundrect ratios and inherited board tenting. Absent native defaults and explicit zero overrides remain distinct from malformed values.

## Full native export geometry

For custom/chamfered pads, per-layer padstacks, mask graphics/text/zones and
merged openings, use the explicit native export API:

```python
from kicad_tools.validate.mask_export_geometry import (
    MaskExportOptions,
    inspect_exported_mask_geometry,
)

snapshot = inspect_exported_mask_geometry(
    "board.kicad_pcb",
    options=MaskExportOptions(board_plot_params=True),
    artifact_dir="mask-review",
)
if not snapshot.complete:
    raise RuntimeError(snapshot.unsupported)
front_openings = snapshot.layers["F.Mask"]
front_copper = snapshot.layers["F.Cu"]
```

This path invokes KiCad 10 on immutable temporary copies. It reconstructs the
**complete exported F.Mask, B.Mask, F.Cu and B.Cu layers**, including mask graphics,
text strokes, custom and chamfered pads, zones, native minimum-web merging, and
per-side copper/padstack geometry. It does not refill zones. The corresponding
copper includes other pads/tracks/fills, so copper between merged openings is not
lost by considering only each pad's own copper. A mask layer represents openings,
not an infinite negative-film polygon. No exposure decision, same-net waiver,
negative-expansion waiver, supplier limit or fabrication qualification is implied.
The original analytic `inspect_mask_geometry` API remains available without a
native executable and continues to report advanced coverage as incomplete.

The snapshot records exact PCB/project/custom-rule hashes, native version,
command and options, command-file hash when available, and exact Gerber hashes.
`artifact_dir` retains the original source files, four Gerbers and a JSON manifest.
The source inventory records UUIDs, object kind, authored layers/position and
parent placement. It is deterministic provenance, **not attribution of merged
layer polygons to individual objects**. Coordinates returned by this API are
native plot coordinates in millimetres with y down; any configured plot origin
applies equally to mask and copper. Source inventory positions retain their
labeled authored frame.

Custom/chamfered/padstack pads also receive `source_geometries`: separately
exported **standalone derivatives** retaining their UUID, footprint transform,
project/rules and plot policy while excluding neighboring geometry. Their source
and Gerber hashes are recorded independently. These identify each source's
standalone native shape; they do not own a merged bridge or predict the full
layer when neighboring openings interact. Only `layers` from the original board
is authoritative for merged geometry. This currently costs one additional native
export per advanced pad. `MaskExportOptions(standalone_sources=False)` explicitly
omits that optional association while retaining complete layer reconstruction.

### Native settings and format profile

Default export mode uses explicit CLI plot defaults; `board_plot_params=True`
uses the board's stored plot policy. Native settings are not reimplemented as
Python precedence guesses. KiCad 10.0.5 controls verify board mask margin,
footprint override, explicit pad zero, negative pad expansion, and distinct
front/back padstack shapes/mask participation. Legacy project JSON
`rules.solder_mask_clearance` does **not** override PCB settings in these native
controls (even when the PCB field is absent); project bytes are nevertheless
retained and hashed. KiCad migrated those legacy project parameters back into
PCB setup. No per-layer mask-margin syntax is invented: unknown padstack fields
are rejected before the permissive native loader can discard them. Explicit
mask layer participation is tested; pad tenting flags that native ignores are
not assigned invented semantics.

`variables=(("SIGN", "text"),)` forwards explicit native plot text variables and
includes them in export identity. Stored board options override CLI aperture
macro flags in KiCad. To keep reconstruction strict, stored-option mode exports
a separately hashed representation-only source copy with
`pcbplotparams.disableapertmacros=true`; all geometry-affecting plot settings
remain intact. Tests compare this macro-free result against an **original,
unmodified native macro export**, using an independent reader. Protel extensions
are accepted and retained bytes are named consistently in the artifact directory.

The strict reader supports absolute leading-zero-suppressed Gerber, millimetres
or inches, standard C/R/O/P flashes, linear strokes, multi-quadrant circular
interpolation, regions and dark/clear polarity. Aperture holes remain transparent;
clear layer operations erase prior geometry. Macros, step-repeat, unsupported
transforms/commands, malformed/truncated streams and unknown padstack fields
produce explicit incomplete coverage. A layer with an unsupported command is
omitted entirely, never returned as a partially reconstructed complete layer.
Missing/malformed/duplicate source UUIDs, native warnings/failures or source
mutation also prevent `complete=True`. Other native major versions require new
qualification; this profile currently accepts KiCad 10.

### Independent export-equivalence controls

`tests/test_mask_export_geometry.py` compares native synthetic custom/rotated,
chamfered, graphic/text, zone, padstack and merged-opening mask/copper Gerbers
against Gerbonara 1.6.3, independently of the production reader. The test oracle
has its own rotated-rectangle and polarity/hole controls. Native controls also
verify a merged opening exposes intervening copper while standalone source
geometry does not claim that bridge, and preserve all original source bytes.

Circular reconstruction uses a **0.000001 mm chord-error budget per
construction**. Export-equivalence comparisons use bidirectional polygon
containment within **0.000005 mm**, plus symmetric-difference area bounded by
that distance times the reference boundary length. This covers both readers'
independent tessellation and 0.000001 mm native coordinate quantization. It is
not a manufacturing clearance. Whole-boundary Hausdorff distance is unsuitable
for vanishingly narrow seams where separately tessellated stroked arcs meet;
the positional containment and area checks still constrain actual material.

Native tests skip explicitly without KiCad; the independent oracle requires
Python 3.12+ (its dependency's minimum). They must actually run to establish
native equivalence. An explicit `native_command` may name a pinned container
invocation; mount its scratch directory at the same absolute path and set
`scratch_dir` if needed. Export commands have a finite configurable timeout.
This completes geometry inputs only; #5063 and the downstream exposure/threshold
checker #5137 still require their own end-to-end acceptance.
