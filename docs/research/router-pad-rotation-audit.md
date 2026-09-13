# Router pad rotation consumer audit (#5194)

This follow-up depends on #5181's residual `Pad.rotation` and both IO parsers.
`load_pcb_for_routing` puts file-loaded pads into `Autorouter.pads`; these reach
escape generation, DRC nudging, `validate_routes`, intra-component path routing,
and zone thermal generation. `load_pads_for_analysis` also preserves the same
residual angle. `SparseRoutingGraph.add_pad` is a public consumer of these pads;
it is not the current two-phase routing backend.

| Consumer | Geometry used |
| --- | --- |
| Nudge `_router_pad_bbox` | Rotation-aware enclosing AABB; existing manufacturer and endpoint anchor behavior retained. Box containment is approximate, not proof of physical copper containment. |
| Escape segment gap | Minimum distance to all edges of the rotated rectangle, rather than projection of its center. The lateral search budget uses directional support of that rectangle. |
| IO segment/via checks | Local-frame rectangular distance with existing layer/net exemptions, comparison tolerances, and signed segment-depth convention. |
| Intra-component paths | Local-frame segment clearance; rotation-aware package perimeter and nearby-pad broad-phase bounds. |
| Sparse graph | Rotation-aware obstacle AABB and external rectangular contour waypoints, including corners. Existing PTH drill/default fallback dimensions retained. |
| Thermal relief | AABB enumeration, rotated-local pad membership, actual rectangle-distance clearance ring, and continuous finite-width spokes through that same ring. |

The residual angle follows IO's existing cardinal dimension swap. KiCad's board
rotation uses the negative mathematical angle, so inverse transforms into the
pad frame use the positive residual angle. Exact cardinal dimensions and
clearance exemptions remain unchanged. Two previous algorithm errors also
required correction: center projection is not the minimum segment-to-rectangle
distance even at cardinal angles; a circular thermal outer boundary based on
`max(width,height)/2 + gap` misses clearance near rectangular corners. Sparse
circular contour samples could likewise land inside rectangular obstacles.
Those corrected cases intentionally change results for some cardinal pads.

These are **rectangular envelopes**: router `Pad` does not retain custom shape,
oval geometry, or corner radius. This work does not claim full shape-aware
copper modeling. Thermal checks classify grid-cell centers; the grid resolution
still limits representation of thin spokes and small gaps. Spoke angles remain
in board coordinates, independent of pad rotation.

The regression suite exercises both file parsers at positive, negative,
near-cardinal, and exact-cardinal angles; compares against independently built
Shapely rectangles using file-loaded schema dimensions and negated absolute
angles; validates segment/via overlap and exact-clearance controls; checks
perimeter and nearby filtering; retains nudge/manufacturer behavior; covers
SMD/PTH/fallback sparse obstacles and waypoints; and traverses thermal spoke
components to verify connection from pad copper through the gap to the pour.

An attempted comparison against the existing validator `_pad_polygon` exposed
an independent sign discrepancy on this parent base: it rotates noncardinal
pad shapes with a positive mathematical angle. That validator issue is tracked
separately; these router predicates retain the verified negative KiCad board
rotation convention rather than reproducing the validator discrepancy.

The independently confirmed validator polygon rotation-sign discrepancy is tracked in [#5227](https://github.com/rjwalters/kicad-tools/issues/5227). It is separate from this router-consumer change.
