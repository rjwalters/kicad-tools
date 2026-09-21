"""Groups 2 and 3 -- the C++ search-time occupancy model, in two halves.

Epic #5509 section 1 splits the C++ search's occupancy into two groups because
they are two *different* clearance models that happen to sit on the same
raster, and a single row could not tell them apart:

============  ===============================================================
group 2       **marking** -- ``Grid3D::mark_segment`` (``grid.cpp:150``) and
              ``Grid3D::mark_via`` (``:226``) stamp a Chebyshev **square** of
              ``clearance_cells`` / ``radius_cells`` around every rastered
              cell.  A square circumscribes the disc it stands for, so the
              marked region reaches ``sqrt(2)`` further at the diagonals than
              the geometry requires.
group 3       **acceptance** -- ``Pathfinder::is_trace_blocked``
              (``pathfinder.cpp:217``) reads those marks back through a
              Euclidean **disc** kernel (issue #3229): only cells with
              ``dx^2 + dy^2 <= r^2`` are consulted.  The same board therefore
              gets a materially *narrower* answer on the read side than on
              the write side.
============  ===============================================================

Both rows are measured on an **unmodified** consumer, driven the way
``tests/router/test_pairwise_cpp_parity.py`` drives it: ``router_cpp.Grid3D``
and ``router_cpp.Pathfinder`` directly, never through ``CppPathfinder.route``
(which would layer a whole A* search, its fixed-fill handling and its off-grid
stored-segment bookkeeping on top of the predicate under test).

**The cell sets come from the consumer, not from a formula re-derived here.**
Both adapters mark the candidate on a second, otherwise identical and empty
grid and *read back* which cells that marking claimed -- so ``mark_segment``'s
own Bresenham walk and its own square halo decide the footprint.  The one
number this module does compute is the radius in cells, because that radius is
computed in *Python* (``cpp_backend.py CppPathfinder.route``) and handed to
C++; it lives in ``_support.trace_radius_cells`` / ``via_radius_cells`` with
its call site cited.

**Why the read-back is windowed.**  ``Grid3D`` exposes no bulk cell view (the
Python grid's ``_blocked`` numpy array has no C++ counterpart), and scanning
473 x 394 x 4 cells per pair through ``at()`` would cost ~750 k Python calls.
The scan is therefore confined to the candidate's own world-space bounding box
grown by the halo radius -- a window derived from *geometry*, not from the
marking rule, so the cell set inside it is still entirely the consumer's.

**Pair kinds are narrowed to routed copper (segment / via).**  The C++ grid
never marks pad copper itself: ``CppGrid.from_routing_grid``
(``cpp_backend.py:923``) copies the *Python* grid's whole blocked plane --
pad metal, pad halo and the ``pad_blocked`` bit included -- with
``np.nonzero`` + ``mark_blocked``, and ``Grid3D::add_pad`` only registers the
pad for the *geometric* branch (``validate_route`` / ``edge_foreign_pad_clear``)
without claiming a single cell.  So a pad's occupancy footprint in the C++
search **is** group 1's, verbatim; scoring ``pad-seg`` / ``pad-via`` here
would re-measure the Python marking under a C++ row's name.  That is exactly
the ``ConsumerAdapter.pair_kinds`` rule -- narrow to what the consumer is
really consulted for -- and it is why these two rows are directly comparable
to each other but only partially to group 1's.

Both rows require the compiled extension (``uv run kct build-native``);
without it ``available()`` is ``False`` and groups 2 / 3 render
``not measured`` rather than a confident zero.

Group 3 additionally answers via candidates through
``Pathfinder::is_via_blocked`` (``bindings.cpp:523``), the sibling acceptance
predicate the A* consults before it will *place* a via.  Restricting the row
to segment candidates would have dropped the ``via-via`` kind, which is
precisely where the square-vs-disc asymmetry is widest.  No
physical route geometry is registered on either grid, so
``route_cell_has_geometry`` is false everywhere and the refinement branch
inside ``is_trace_blocked`` stays inert -- that refinement is group 5's row,
measured separately in ``route_geometry_cpp.py``.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    cpp_grid_for,
    layer_indexer,
    net_ids,
    pair_contexts,
    router_cpp_module,
    router_pad,
    router_rules,
    router_segment,
    router_via,
    trace_radius_cells,
    via_radius_cells,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec

#: Routed-copper pair kinds only -- see the module docstring on why pad pairs
#: are out of scope for the C++ marking model.
ROUTED_COPPER_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.VIA_VIA})

__all__ = ["CppTraceBlockedAdapter", "GridCppMarkingAdapter"]


def _register_existing(router_cpp, grid, existing, nets, rules, layer_index) -> None:
    """Put the pair's counterpart on ``grid`` as the search would see it.

    A pad is static blockage (``add_pad``, which also records the pad for the
    geometric branch); a segment or via is routed copper and is *marked* with
    the same halo radius the search uses.
    """
    del router_cpp
    if isinstance(existing, PadSpec):
        pad = router_pad(existing, nets)
        grid.add_pad(
            pad.x,
            pad.y,
            pad.width,
            pad.height,
            pad.net,
            layer_index(existing.layer),
            0,  # ref_hash: no same-component carve-out applies here
            rules.get_clearance_for_component(pad.ref),
            False,  # is_plane_net
            pad.rotation,
            pad.shape == "circle",
        )
    elif isinstance(existing, SegmentSpec):
        seg = router_segment(existing, nets)
        gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
        grid.mark_segment(
            gx1,
            gy1,
            gx2,
            gy2,
            layer_index(existing.layer),
            seg.net,
            trace_radius_cells(rules),
        )
    else:
        via = router_via(existing, nets)
        gx, gy = grid.world_to_grid(via.x, via.y)
        grid.mark_via(gx, gy, via.net, via_radius_cells(rules))


def _candidate_cells(grid, candidate, nets, rules, layer_index, radius: int):
    """Cells the candidate's own marking claims, as ``(layer, gy, gx)``.

    ``radius`` is passed straight to the consumer's marking call: the halo
    radius for group 2's write-side footprint, ``0`` for group 3's bare
    raster.  The returned set is read back out of the grid the consumer just
    wrote, never computed here.
    """
    net = nets[candidate.net]
    if isinstance(candidate, SegmentSpec):
        seg = router_segment(candidate, nets)
        gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
        layer = layer_index(candidate.layer)
        grid.mark_segment(gx1, gy1, gx2, gy2, layer, net, radius)
        reach = seg.width / 2.0
        box = (
            min(seg.x1, seg.x2) - reach,
            min(seg.y1, seg.y2) - reach,
            max(seg.x1, seg.x2) + reach,
            max(seg.y1, seg.y2) + reach,
        )
        layers = (layer,)
    else:
        via = router_via(candidate, nets)
        gx, gy = grid.world_to_grid(via.x, via.y)
        grid.mark_via(gx, gy, net, radius)
        reach = via.diameter / 2.0
        box = (via.x - reach, via.y - reach, via.x + reach, via.y + reach)
        # ``mark_via`` claims cells on *every* copper layer -- a through via
        # reaches all of them, which is the whole reason a via halo is so
        # expensive for the search.
        layers = tuple(range(grid.layers))

    # Window: the candidate's own copper extent, grown by the halo radius.
    # Geometry, not the marking rule -- see the module docstring.
    lo_x, lo_y = grid.world_to_grid(box[0], box[1])
    hi_x, hi_y = grid.world_to_grid(box[2], box[3])
    pad_cells = radius + 1
    cells: list[tuple[int, int, int]] = []
    for layer in layers:
        for gy in range(lo_y - pad_cells, hi_y + pad_cells + 1):
            for gx in range(lo_x - pad_cells, hi_x + pad_cells + 1):
                if not grid.is_valid(gx, gy, layer):
                    continue
                if grid.at(gx, gy, layer).blocked:
                    cells.append((layer, gy, gx))
    return cells


def _required_for(candidate, rules) -> float:
    return rules.trace_clearance if isinstance(candidate, SegmentSpec) else rules.via_clearance


class GridCppMarkingAdapter:
    """Group 2 -- the write side: ``mark_segment`` / ``mark_via`` squares.

    Rejects a candidate when any cell its own marking would claim is already
    claimed by foreign copper.  Structurally identical to group 1's
    ``occupancy`` adapter, so the two rows are directly comparable: the only
    difference between them is which language marked the cells.
    """

    name = "grid_cpp_marking"
    group = 2
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
            candidate = context.candidate
            radius = (
                trace_radius_cells(rules)
                if isinstance(candidate, SegmentSpec)
                else via_radius_cells(rules)
            )

            board = cpp_grid_for(router_cpp, case, rules)
            _register_existing(router_cpp, board, context.existing, nets, rules, layer_index)

            probe = cpp_grid_for(router_cpp, case, rules)
            cells = _candidate_cells(probe, candidate, nets, rules, layer_index, radius)

            if _collides(board, cells, nets[candidate.net]):
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # a cell set has no millimetre answer
                        required_mm=_required_for(candidate, rules),
                    )
                )
        return found


class CppTraceBlockedAdapter:
    """Group 3 -- the read side: the Euclidean-disc acceptance kernel.

    Marks the counterpart exactly as group 2 does, then asks the *search's*
    own predicate about each cell the candidate's bare raster occupies:
    ``Pathfinder::is_trace_blocked`` for a segment candidate,
    ``Pathfinder::is_via_blocked`` for a via.  A rejection here means A* will
    not step onto (or drop a via at) this pair even when the copper is legal.
    """

    name = "cpp_blocked_kernel"
    group = 3
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

            board = cpp_grid_for(router_cpp, case, rules)
            _register_existing(router_cpp, board, context.existing, nets, rules, layer_index)
            pathfinder = _pathfinder_for(router_cpp, board, rules)

            candidate = context.candidate
            probe = cpp_grid_for(router_cpp, case, rules)
            # Radius 0: the bare raster the candidate occupies, so the disc
            # kernel inside the predicate is the only halo in play.
            cells = _candidate_cells(probe, candidate, nets, rules, layer_index, 0)
            net = nets[candidate.net]

            blocked = False
            for layer, gy, gx in cells:
                if isinstance(candidate, SegmentSpec):
                    blocked = pathfinder.is_trace_blocked(gx, gy, layer, net, False)
                else:
                    blocked = pathfinder.is_via_blocked(gx, gy, net, False)
                if blocked:
                    break

            if blocked:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,
                        required_mm=_required_for(candidate, rules),
                    )
                )
        return found


def _pathfinder_for(router_cpp, grid, rules):
    """A ``Pathfinder`` over ``grid`` carrying the case's own rule values.

    Only the fields the two acceptance predicates read are set:
    ``trace_half_width_cells_`` (derived in the constructor from
    ``trace_width`` / ``trace_clearance`` / the grid resolution) and the via
    envelope.  Building ``router_cpp.DesignRules`` directly rather than going
    through ``CppPathfinder.__init__`` keeps manufacturer limits, net-class
    maps and iteration caps -- none of which touch clearance -- out of the
    measurement.
    """
    cpp_rules = router_cpp.DesignRules()
    cpp_rules.trace_width = rules.trace_width
    cpp_rules.trace_clearance = rules.trace_clearance
    cpp_rules.via_diameter = rules.via_diameter
    cpp_rules.via_drill = rules.via_drill
    cpp_rules.via_clearance = rules.via_clearance
    cpp_rules.min_hole_to_hole = rules.min_hole_to_hole
    cpp_rules.min_drill_clearance = rules.min_drill_clearance
    cpp_rules.grid_resolution = rules.grid_resolution
    return router_cpp.Pathfinder(grid, cpp_rules, True)


def _collides(grid, cells, net: int) -> bool:
    """True when any cell the candidate would claim is foreign-blocked.

    Mirrors ``CppGrid.is_blocked_for_net`` (``cpp_backend.py:1184``): a cell
    holding the candidate's *own* net is passable, net-0 obstacles and foreign
    copper are not.
    """
    for layer, gy, gx in cells:
        if not grid.is_valid(gx, gy, layer):
            return True
        cell = grid.at(gx, gy, layer)
        if cell.blocked and (cell.net == 0 or cell.net != net):
            return True
    return False
