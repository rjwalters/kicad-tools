# MCP clearance and router grid audit (#5128)

`measure_clearance` now adapts parsed PCB elements to the shared validator's
`CopperElement` and pair dispatcher. Footprint-local positions, absolute pad
angles, shapes, roundrect ratios, and available polygons follow that shared
path. MCP keeps its public `track` name and `Track-` references, layer selection,
item matching, nonzero same-net exemption, net-zero comparisons, minimum
selection, and result structure. The unrounded minimum still determines whether
the unchanged required clearance is met. Pair locations come from the shared
geometry: polygon pairs may report a closest-edge location rather than the old
center midpoint.

All pair types delegate: track/track, track/pad, track/via, pad/pad, pad/via, and
via/via, including reversed operands. Missing pad polygons cause an explicit
error instead of a silent disc fallback. `get_drc_violations` already uses
`DRCChecker` and was not changed.

This implementation depends on #5155's rounded-pad segment correction and
#5227's polygon rotation-sign correction for physical noncardinal results.
It does not implement another polygon engine. Shared-model limitations remain:
plain rectangular segment/pad checks retain their analytic AABB behavior;
unknown pad shapes fall back to rectangles; polygon overlap depth is a modeled
penetration estimate. This is not universal exact KiCad geometry or custom-pad
support, and does not certify manufacture.

## Router grid disposition

`router/grid.py::_rect_segment_centerline_distance` and its consumers are
unchanged. The audit found calls in `worst_segment_pad_deficit`,
`worst_via_pad_deficit`, and `validate_segment_clearance`: these affect routing
and finalization acceptance/backstops, not just search performance.

For an axis-aligned, non-square oval or roundrect, its enclosing rectangle is a
conservative restriction around rounded corners. Keeping that restriction here
avoids changing routing cost and backstop behavior while fixing MCP diagnostics.
However, this does **not** establish that every grid pad model is conservative:
the consumers infer circular pads from nearly equal width and height, which
under-covers square rectangular corners. Discarded noncardinal orientation also
under-covers real copper. Those distinct correctness risks are tracked in
[#5229](https://github.com/rjwalters/kicad-tools/issues/5229), covering shape/rotation
propagation, Python/C++ agreement, and finalization regressions. #5194's other router consumers do not cover these grid sites.

No grid geometry, manufacturing thresholds, clearance floors, routing guards,
or finalization exclusions were changed by this MCP adapter. The tests establish
MCP/shared-model parity and independent physical controls for supported cases;
they do not claim new router-runtime validation.
