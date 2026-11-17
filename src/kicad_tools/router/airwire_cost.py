"""Prospective airwire-separation cost plane (Issue #8999).

Every unrouted connection has a naive desired route: the straight line
(flight line) between its endpoints.  The negotiated cost model's two
existing terms are *retrospective* -- ``present_cost`` and ``history_cost``
only rise once a cell has already been over-used by committed copper --
so nothing prices a cell that sits astride a connection nobody has
attempted yet.  An early net takes that corridor and a later net pays for
it with a detour, a via or an unroutable connection (the epic #5509
ordering class).  This module adds the missing *prospective* term: a
per-cell count of how many still-unrouted nets' flight lines pass through
the cell, summed into ``get_negotiated_cost`` (weighted by a flag-supplied
scalar) so a candidate path that slips around other nets' pending desire
lines is preferred to one that cuts across them.

Geometry reuse (ideas-only provenance, Issue #8999: WeaveEngine is
GPL-3.0, this repo is MIT -- implemented from the behaviour description,
never from upstream code):

* per-net desire lines are the Euclidean minimum-spanning skeleton over
  pad centres -- :func:`kicad_tools.router.crossing_order.minimum_spanning_skeleton`,
  the same reduction ``--order-method crossing`` uses (Issue #5787);
* each segment is rasterised with ``RoutingGrid._get_segment_cells``
  (a layer-bound Bresenham; the layer is dropped because a desire line
  is layer-less -- the count column applies to every layer).

Determinism (the issue's ground rule): the plane is a **pure function of
the committed set**.  It is recomputed from scratch on every refresh --
never incrementally accumulated -- and counts are accumulated with
integer ``+=`` on a zeroed array, so the result is bit-identical for any
rasterisation or net iteration order.  A float32 plane would make the
sum order-sensitive in principle; the plane therefore counts crossings in
an integer array and the float weight is applied at read time.  When two
candidate paths have equal total cost the existing A* tie-break
(``IndexedAStarQueue``'s g-score/insertion-order rule) applies unchanged;
this term introduces no new tie-break rule.

The term is a cost, never a passability change: it must not make a
blocked cell look passable (see ``RoutingGrid.get_negotiated_cost``, which
hard-rejects obstacle cells before the term is ever added).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import numpy as np

from .crossing_order import minimum_spanning_skeleton
from .layers import Layer
from .primitives import Segment

if TYPE_CHECKING:
    from .core import Autorouter
    from .grid import RoutingGrid


def _desire_line_cells(
    grid: RoutingGrid, p1: tuple[float, float], p2: tuple[float, float]
) -> set[tuple[int, int]]:
    """Rasterise one desire line to its (x, y) grid cells.

    Reuses ``RoutingGrid._get_segment_cells`` (the layer-bound Bresenham
    the router itself uses) with a throwaway layer -- a desire line has no
    layer, so the layer component of the returned cells is discarded and
    the same column is applied to every layer by the caller.
    """
    seg = Segment(x1=p1[0], y1=p1[1], x2=p2[0], y2=p2[1], width=0.0, layer=Layer.F_CU, net=0)
    return {(gx, gy) for gx, gy, _layer in grid._get_segment_cells(seg)}


def unrouted_net_skeletons(
    router: Autorouter, *, exclude_net: int | None = None
) -> dict[int, list[tuple[tuple[float, float], tuple[float, float]]]]:
    """Desire-line skeletons for every net the negotiated loop has left to route.

    A net contributes desire lines while (and only while) it has no
    committed route: the negotiated loop rips copper up by unmarking it,
    so a ripped net re-enters the unrouted set and its flight lines steer
    the search that re-lands it -- exactly the "pure function of the
    committed set" contract.  Excluded up front: the net about to be
    routed (its own desire line must not price its own straight path --
    the behaviour counts *other* nets' lines only), pour nets (connected
    by zone fills, never by the negotiated loop), net 0 and single-pad
    nets (no connection to desire).

    Pad centres are the endpoints, the same prior
    ``--order-method crossing`` uses; escape overrides and stub terminals
    are approximated away (a desire line is a prior, not a route).
    """
    skeletons = router.__dict__.get("_airwire_skeleton_cache")
    if skeletons is None:
        skeletons = {}
        router._airwire_skeleton_cache = skeletons

    routed_nets = {route.net for route in router.routes}
    result: dict[int, list[tuple[tuple[float, float], tuple[float, float]]]] = {}
    for net_id in sorted(router.nets):
        if net_id == 0 or net_id == exclude_net or net_id in routed_nets:
            continue
        if router._is_pour_net(net_id):
            continue
        cached = skeletons.get(net_id)
        if cached is None:
            pad_keys = router.nets.get(net_id, [])
            if len(pad_keys) < 2:
                cached = []
            else:
                points = [
                    (pad.x, pad.y) for key in pad_keys if (pad := router.pads.get(key)) is not None
                ]
                cached = minimum_spanning_skeleton(points)
            skeletons[net_id] = cached
        if cached:
            result[net_id] = cached
    return result


def compute_airwire_crossing_plane(
    grid: RoutingGrid,
    skeletons: dict[int, list[tuple[tuple[float, float], tuple[float, float]]]],
) -> np.ndarray:
    """Build the integer crossing-count plane from per-net desire-line skeletons.

    ``plane[gy, gx]`` is the number of *distinct nets* whose skeleton
    covers the cell (a net's own overlapping skeleton segments count once:
    per-net cells are deduplicated in a set before the ``+= 1``).  Counts
    are integers accumulated with ``+=`` so the plane is order-independent
    and bit-identical however the nets or segments are iterated.
    """
    plane = np.zeros((grid.rows, grid.cols), dtype=np.int16)
    for net_id in sorted(skeletons):
        cells: set[tuple[int, int]] = set()
        for p1, p2 in skeletons[net_id]:
            cells |= _desire_line_cells(grid, p1, p2)
        if not cells:
            continue
        xs = np.fromiter((gx for gx, _gy in cells), dtype=np.intp, count=len(cells))
        ys = np.fromiter((gy for _gx, gy in cells), dtype=np.intp, count=len(cells))
        # Pad geometry is in-bounds by construction and Bresenham between
        # in-bounds endpoints never leaves their bounding box; the mask is
        # a defensive clip for hand-built fixtures.
        in_bounds = (xs >= 0) & (xs < grid.cols) & (ys >= 0) & (ys < grid.rows)
        plane[ys[in_bounds], xs[in_bounds]] += 1
    return plane


def refresh_airwire_cost(
    router: Autorouter, *, weight: float, current_net: int | None = None
) -> int:
    """Recompute the airwire-separation plane from the current committed set.

    Writes the integer count plane onto ``router.grid._airwire_cost`` (the
    pure-Python A* reads it through ``RoutingGrid.get_negotiated_cost``,
    which applies ``grid._airwire_weight`` at read time) and pushes the
    weighted sparse cells onto the C++ grid (``GridCell::airwire_cost``)
    so all four C++ A* cost sites price the same term.  The C++ push is a
    full clear + rewrite, never an increment, keeping the native plane a
    pure function of the committed set too.

    Returns the number of cells covered by at least one desire line (0
    with the flag off -- the plane stays all-zero and the cost term is
    exactly ``+0.0``).
    """
    grid = router.grid
    cpp_grid: Any = getattr(router, "_cpp_grid", None)
    impl = getattr(cpp_grid, "_impl", None) if cpp_grid is not None else None

    grid._airwire_weight = float(weight)
    if weight <= 0.0:
        # Flag off: never build the plane; both grids keep all-zero marks
        # so the negotiated cost is byte-identical to the pre-#8999 value.
        # A no-op unless a previous refresh actually marked something (a
        # re-entered driver that turned the flag off between runs).
        if router.__dict__.get("_airwire_cost_pushed"):
            if getattr(grid, "_airwire_cost", None) is not None:
                grid._airwire_cost[...] = 0
            if impl is not None:
                impl.clear_airwire_costs()
            router._airwire_cost_pushed = False
        return 0

    skeletons = unrouted_net_skeletons(router, exclude_net=current_net)
    plane = compute_airwire_crossing_plane(grid, skeletons)
    target = getattr(grid, "_airwire_cost", None)
    if target is not None:
        target[...] = plane

    # Harness guard (Issue #8999), printed from the refresh itself rather
    # than ``route_all_negotiated`` because the two-phase corridor funnel
    # never passes through that entry point -- the A/B arms assert this
    # line so a dispatch path that silently drops the flag is never read
    # as a measurement (the #5787 silent no-op lesson).  Once per router.
    if not router.__dict__.get("_airwire_log_printed"):
        router._airwire_log_printed = True
        from kicad_tools.cli.progress import flush_print

        flush_print(
            f"  Airwire separation cost: weight={weight:g} "
            f"(unrouted desire-line crossings priced per cell)"
        )

    if impl is not None:
        impl.clear_airwire_costs()
        ys, xs = np.nonzero(plane)
        if xs.size:
            costs = (weight * plane[ys, xs].astype(np.float32)).astype(np.float32)
            impl.set_airwire_cells(xs.tolist(), ys.tolist(), costs.tolist())
            router._airwire_cost_pushed = True
        else:
            router._airwire_cost_pushed = False
    return int(np.count_nonzero(plane))
