# Placement-excluded fixed copper

`load_pcb_for_routing(..., placement_disposition=...)` retains the copper of
placement-excluded source nets independently of routing selection. This loader
API does not activate the default CLI policy; issues #5347 and #5348 consume it.

`router.grid.fixed_fills` is an immutable `FixedFillObstacles` collection.
Each entry carries the authored net name/number, physical grid layer index,
source-zone clearance, geometry in sheet coordinates, zone UUID, and source
kind/object UUID where applicable. Effective net remapping never grants reuse
of this copper, including when a source net is split into invalid and eligible
nets. Segment, point and via predicates use actual polygon distance and holes;
bounds and native spatial bins only accelerate rejection. Required distance is
query half-width plus the larger of query and source clearances, with the same
0.0001 mm clearance tolerance as the existing grid checks. Copper intersections
are always forbidden.

Filled polygons retain every repaired lobe and interior ring. Older stroked
polygon fills and segment-only fills follow KiCad's `min_thickness` and
`filled_areas_thickness` semantics. Raw arcs remain unchanged; their physical
obstacles use a circular centerline tessellation with a 0.00001 mm sagitta bound
and an outward-rounded stroke. This bounded approximation preserves copper,
without replacing curved geometry with its bounding box. The source-format
semantics follow the [KiCad S-expression parser](https://github.com/KiCad/kicad-source-mirror/blob/master/pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp).

Python/native grid searches and final route checks, lattice single/coupled
routing, and mesh routing consult these physical obstacles. Trial reset,
parallel-grid conversion and fine-grid retries carry the collection forward.
`validate_routes` reports violations with `obstacle_type="fixed_copper"` and
the authored source net identity.

The export handoffs have distinct ownership:

- `placement_preserved_copper` contains authored segment/via blocks that the
  writer strips and must insert exactly once.
- `placement_preserved_zones` and `placement_preserved_arcs` contain exact source
  blocks already retained by the writer. Do not append them again.
- `placement_preserved_routes` carries source segment/via identities; neutral
  obstacle copies remain in `existing_routes` even without load-existing mode.

Consumers that refill zones must freeze the preserved source blocks or restore
and verify their exact contents after refill. This prerequisite provides the
handoff; default CLI activation and refill orchestration remain #5348 work.

## Placement-excluded custom pads

A custom pad whose **effective** net is placement-invalid contributes its actual
copper as a fixed obstacle instead of refusing (issue #5357). Such a pad becomes
no `Pad`, no routing target and carries no pour intent: it is only physical
copper, entered in the same `fixed_fills` collection with `source_kind="pad"`
and `source_object_id="<REF>.<PAD>"`. The authored net name/number are retained
even when a netlist override splits the source net, and effective-net remapping
never grants reuse. Clearance is the larger of the trace clearance, the pad's
own `(clearance ...)` and the source net class. The source `(pad ...)` block is
read but never rewritten.

Copper is the union of the authored anchor and the pad's supported primitives,
rotated once by the pad's absolute board-frame angle (KiCad folds the footprint
orientation into `(at x y ANGLE)`) and translated to the pad's board position --
the same forward transform as `validate.rules.clearance._pad_polygon`, so
back-side and non-cardinal footprints resolve correctly. Concavity and every
lobe survive; no nominal `(size ...)` box or enclosing box is ever substituted.
Layer coverage uses the pad's copper layers only (`*.Cu` expands to the whole
stack; paste/mask layers are not obstacles).

The supported surface is deliberately narrow, and everything outside it refuses
rather than approximating:

- anchors: `rect`, or `circle` with equal size;
- primitives: filled `gr_poly` with zero stroke width;
- refused: any other primitive (`gr_circle`, `gr_line`, `gr_arc`, `gr_curve`,
  `gr_rect`), unfilled or unrecognized fills, non-zero stroke widths,
  layer-specific padstacks, non-positive sizes, pads with no copper layer, and a
  copper layer absent from the routing stack.

Routable (non-excluded) custom pads and every other caller of
`_pad_shape_from_block` / `_schema_pad_shape` -- standalone pad parsing,
`optim/place_route.py`, `mcp/tools/routing.py`, `cli/placement_cmd.py` -- keep
rejecting custom geometry exactly as before. Support for fixed excluded copper
is not custom-pad target routing.

On the pinned BeagleConnect Freedom input, custom pad U6.9 (B.Cu, GND, rect
anchor, 270-degree pad rotation, one filled zero-width `gr_poly`) is now
preserved as its real transformed copper, 0.115 mm^2 of which lies outside the
nominal `(size 0.2 1)` box in isolated geometry diagnostics. This does not
establish acceptance through the full pinned loader. The empty pad-number
identity fix (#5368) is incorporated, but circular and curved/arc Edge.Cuts
support (#5367/#5372) still blocks the unchanged board before pad loading.
The pinned acceptance test preserves the complete outline and actual placement
identities and remains failing at that guard when the fixture is available.
Full loader acceptance remains unresolved until those prerequisites land.
