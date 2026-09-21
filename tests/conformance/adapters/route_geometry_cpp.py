"""Group 5 -- the C++ search-time geometric refinement, unmodified.

The C++ counterpart of group 4's ``RouteHaloGeometry.clear``: when the raster
says a cell is blocked, the search asks *this* predicate whether the block is
a real geometric conflict or merely halo.  Three entry points, all bound
(``bindings.cpp:221`` / ``:222`` / ``:295``):

* ``Grid3D::route_trace_geometry_clear`` (``grid.cpp:929``) -- a candidate
  trace against stored segments and stored vias.
* ``Grid3D::route_via_geometry_clear`` (``grid.cpp:996``) -- a candidate via
  against stored segments and stored vias, copper **and** drill.
* ``Grid3D::trace_stored_vias_clear`` (``grid.cpp:903``) -- the hard
  trace-vs-stored-via constraint the negotiated path applies on top of the
  first (foreign trace copper stays soft there; via copper does not).

The two ``route_*_geometry_clear`` bindings are **positional-only** (no
``_a`` names at ``bindings.cpp:221-222``), so they are called positionally
here, in the declared order.

**The row measures the Grid3D predicate, not the Pathfinder wrapper.**  The
methods the live search actually calls -- ``Pathfinder::trace_halo_cell_clear``
(``pathfinder.cpp:190``) and ``via_route_geometry_clear`` (``:206``) -- are
*not* bound.  They add only three things before delegating to the two Grid3D
methods above: the cell-to-world conversion, the ``search_fill_*`` per-net
clearance overrides, and a ``route_cell_has_geometry`` pre-check.  None of
those is arithmetic; the arithmetic is entirely in ``Grid3D``.  Phase 1c
deliberately does not add a binding to reach the wrapper (the epic's "no C++
in this phase" guard), so the delta this row does not see is:

* per-net ``search_fill_trace_clearance_`` / ``search_fill_via_clearance_``
  substitution -- inert here, because every net in a generated case takes the
  same scalar rule value; and
* the ``route_cell_has_geometry`` gate, which can only make the wrapper
  *stricter* (an unknown cell is refused outright), never more permissive.

**Expected disagreement: the ``max(via_clearance, ...)`` widening.**  For a
*trace* candidate against a stored via, ``route_trace_geometry_clear``
compares the gap against ``max(via_clearance, required)`` -- the via rule, not
the trace rule -- which is the same search-side widening group 4's row
carries and which the ``search-vs-commit-seg-via-max`` fixture reproduces.
The commit gates (groups 12 / 13) use ``trace_clearance`` for the same pair,
so the two stages disagree about identical copper.

**Pair kinds: routed copper only.**  Neither ``route_*_geometry_clear``
consults ``pads_`` -- they walk ``stored_segments_`` and ``stored_vias_`` and
nothing else (``grid.cpp:929-1050``).  Pad copper reaches the search through
the raster (groups 1-3) and the commit gate's own pad branch (group 13), so
counting ``pad-seg`` / ``pad-via`` here would record the absence of a check
that lives elsewhere.

Requires the compiled extension; without it group 5 renders ``not measured``.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    cpp_grid_for,
    cpp_segment,
    cpp_via,
    layer_indexer,
    net_ids,
    pair_contexts,
    router_cpp_module,
    router_rules,
    router_segment,
    router_via,
    trace_radius_cells,
    via_radius_cells,
)
from tests.conformance.generator import CopperCase, PairKind, SegmentSpec

__all__ = ["RouteGeometryCppAdapter"]

#: ``route_*_geometry_clear`` sees stored segments and stored vias only.
ROUTED_COPPER_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.VIA_VIA})

#: ``partner_net`` / ``partner_clearance`` sentinels for "no diff-pair partner"
#: -- the defaults the non-coupled search passes (``grid.hpp:290``, ``:295``).
NO_PARTNER_NET = -1
NO_PARTNER_CLEARANCE = -1.0


class RouteGeometryCppAdapter:
    """Drives the three ``Grid3D`` route-geometry predicates on a bare grid."""

    name = "route_geometry_cpp"
    group = 5
    pair_kinds = ROUTED_COPPER_KINDS

    def available(self) -> bool:
        return router_cpp_module() is not None

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        router_cpp = router_cpp_module()
        if router_cpp is None:  # pragma: no cover - guarded by available()
            return set()

        nets = net_ids(case)
        rules = router_rules(case)
        layer_index = layer_indexer(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue

            grid = cpp_grid_for(router_cpp, case, rules)
            existing = context.existing
            # Existing copper is registered exactly as a completed route is:
            # ``mark_*`` writes the raster and ``add_stored_*`` registers the
            # physical geometry under the *same* grid key.  Both halves are
            # mandatory -- ``route_geometry_complete()`` is false on a grid
            # with no marks at all (``grid.cpp:579``), which would make the
            # live refinement decline rather than answer.
            if isinstance(existing, SegmentSpec):
                seg = router_segment(existing, nets)
                layer = layer_index(existing.layer)
                gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
                gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
                grid.mark_segment(gx1, gy1, gx2, gy2, layer, seg.net, trace_radius_cells(rules))
                grid.add_stored_segment(
                    seg.x1,
                    seg.y1,
                    seg.x2,
                    seg.y2,
                    seg.width,
                    layer,
                    seg.net,
                )
            else:
                via = router_via(existing, nets)
                gx, gy = grid.world_to_grid(via.x, via.y)
                grid.mark_via(gx, gy, via.net, via_radius_cells(rules))
                grid.add_stored_via(
                    via.x,
                    via.y,
                    via.drill,
                    via.diameter,
                    via.net,
                    None,
                    layer_index(existing.layers[0]),
                    layer_index(existing.layers[1]),
                )

            # The live gate (``pathfinder.cpp:814``) refuses to refine at all
            # unless every active mark has registered geometry.  Assert it
            # rather than assume it: a silently-false reading here would make
            # the row measure "refinement declined" instead of "refinement
            # disagreed", and the two are opposite conclusions.
            assert grid.route_geometry_complete(), (
                "the marked counterpart has no registered geometry -- the "
                "refinement gate would decline, not answer"
            )

            candidate = context.candidate
            if isinstance(candidate, SegmentSpec):
                probe = cpp_segment(router_cpp, candidate, nets, layer_index)
                clear = grid.route_trace_geometry_clear(
                    probe,
                    rules.trace_clearance,
                    NO_PARTNER_NET,
                    NO_PARTNER_CLEARANCE,
                    rules.via_clearance,
                ) and grid.trace_stored_vias_clear(
                    probe,
                    rules.trace_clearance,
                    NO_PARTNER_NET,
                    NO_PARTNER_CLEARANCE,
                )
                required = rules.trace_clearance
            else:
                probe = cpp_via(router_cpp, candidate, nets, layer_index)
                clear = grid.route_via_geometry_clear(
                    probe,
                    rules.via_clearance,
                    rules.min_hole_to_hole,
                    rules.min_drill_clearance,
                )
                required = rules.via_clearance

            if not clear:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the predicate answers yes/no, not a distance
                        required_mm=required,
                    )
                )
        return found
