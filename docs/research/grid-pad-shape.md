# Router pad shape coverage

Issue #5229 separates physical circles from square rectangles in Python grid
clearance validation, via/segment deficit backstops, and native acceptance.
Shape is retained through both PCB loading paths, component dictionaries,
worker serialization, schema-backed builders, and physical pad copies.

The router `Pad.shape` defaults to `rect` when a programmatic caller supplies
no shape. Callers modeling an actual circle must pass `shape="circle"`.
Equal dimensions alone no longer imply a circle. Authored circles use a disc;
rectangles use the residual KiCad rotation convention established in #4910
and #5182. Oval and roundrect pads currently use a conservative enclosing
rectangle, including at their rounded corners. This can reject a route that
exact rounded geometry would allow.

Search obstacles use board-space enclosing bounds. Both pad insertion entry
points register geometry and synchronize native state. Native interface 23
adds explicit circle classification; rebuild with `kct build-native --force`.
Version equality with another branch is not evidence of compatible native
interfaces; compare the binding and source changes during integration.

Custom pads, trapezoids, unknown shapes, and layer-specific padstacks are
rejected before their source geometry can be reduced to nominal width/height.
Those dimensions do not prove an enclosing bound for arbitrary copper. This
is an explicit coverage limit, not a request to change a board's physical pads.
A router with the relevant full-copper support is needed for those inputs.

The change preserves authored clearance floors, layer spans, nonzero same-net
exemptions, component policies, and finalization's existing 0.05 mm nudge reach.
Tests include file-backed inputs, actual finalization, compiled native checks,
positive circle controls, rotated rectangles, and exact-limit comparisons.
These are geometry/routing controls, not native KiCad DRC or manufacturing
qualification of a generated board.
