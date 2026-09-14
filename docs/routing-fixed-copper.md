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
Unsupported custom-pad/padstack geometry continues to raise the existing loader
error. In particular, the pinned BeagleConnect input still contains unsupported
custom pad U6.9; placement exclusion does not waive that physical-geometry check.
