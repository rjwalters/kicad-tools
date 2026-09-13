# Grid pad rotation acceptance (#5182)

The grid's three Python pad checks and native segment/via acceptance use the
pad's local rectangular axes. KiCad positive angles rotate copper by the
negative mathematical angle in board XY coordinates; the inverse transform
uses the positive residual angle. The #4910 parser's cardinal width/height swap
is retained, and zero residual angles take the original arithmetic path.
An enclosing AABB is useful for grid blockage but cannot determine physical
clearance: it also covers empty space beside a rotated rectangle.

Native build v22 carries rotation through initial population and late pad
registration. Candidate vias now check foreign pads even when the candidate
route contains no segments. Violation code 8 means via-pad; code 7 remains
reserved for the search's pairwise-blocked reason. The violation location is
the pad center. Old native builds are disabled by the existing version guard.

Via-pad acceptance follows `worst_via_pad_deficit`: inclusive via layer spans,
through-hole pads on every layer, same-net exclusion, and the component's trace
clearance floor. Other native via quadrants retain their separate via floors.
Only eligible relaxed/fine-pitch component references may waive a nonnegative
via-pad gap; plane pads and copper overlap (including net 0) are never waived.
Eligibility and clearance are refreshed lazily before native via acceptance
when late pad additions, rules or corridor relaxation change. Repeated checks
reuse the grid pitch cache and policy snapshot; segment-only routes skip this
work.
The preexisting native segment carve-out policy is unchanged.

These are the grid's existing rectangular/disc approximations, not arbitrary
KiCad pad-shape geometry. Zero-residual equal-width/height pads still use the
legacy disc heuristic. Shape discrimination, including square pads, is tracked
separately in [#5229](https://github.com/rjwalters/kicad-tools/issues/5229).
Rounded rectangles and ovals continue to use the rectangle envelope where
width differs from height; this change does not claim exact shape fidelity.

Regression points are constructed independently from original 4-by-1 pad
coordinates using the negative full KiCad angle. Tests exercise signed,
cardinal and near-cardinal angles; true overlaps and sub-floor gaps; valid
points within the enclosing AABB; initial and late native registration;
policy exclusions; and actual Autorouter finalization with the unchanged
0.127 mm floor and 0.05 mm nudge reach. Native tests run against a locally
compiled extension and skip explicitly when that backend is unavailable.
