"""Regret-ordered net committing (Issue #9001).

Every order already on the ``--order-method`` menu ranks nets by something
other than what a net stands to lose: pad count, net class, a congestion map
(``RoutingOptimizer`` heuristics) or flight-line crossing degree
(``crossing_order.py``).  None of them distinguishes a net with exactly one
way through from a net with ten equally good ways through -- yet the first
must be committed early and the second can safely adapt to whatever is left.

This module ranks by **regret**: ``cost(second-best) - cost(best)``, measured
per net on the prepared grid before any copper is committed (the two
empty-board probes live in the CLI bridge, :func:`kicad_tools.cli.route_cmd._regret_net_order`;
this module is pure ranking + pure geometry so it stays unit-testable).  A net
whose second probe finds no path has only one corridor -- regret is +inf and
it sorts first within its band.  A net whose first probe fails cannot be
measured -- its regret is 0.0 and it falls through to the existing priority
tiebreak, never promoted or demoted on the strength of a failed probe.

The ordering is applied *within* each net-class priority band, never across
one: class priority (clock/diff-pair before digital before default) is an
invariant of :meth:`Autorouter._get_net_priority` that this heuristic must not
silently invert -- exactly as :func:`kicad_tools.router.crossing_order.crossing_aware_order`
does for crossing degree.  Ties inside a band fall back to the caller's
existing priority key, so the order degrades to the default sort whenever all
regrets are equal.

Pure geometry + combinatorics: no grid, router or IO dependency.  Ideas-only
provenance (WeaveEngine evaluation, Issue #8993): implemented from the
behaviour description in Issue #9001, never from upstream code.
"""

from __future__ import annotations

import math

from .primitives import Route

#: Sentinel regret returned when the *first* probe finds no path: the
#: measurement is unknown, not "infinite regret".  The bridge translates this
#: convention -- the net keeps its default position via the priority tiebreak.
UNKNOWN_REGRET = 0.0


def route_probe_cost(route: Route, via_cost: float) -> float:
    """Geometric cost of a probe route in the A*'s own cost units.

    ``total segment length (mm) + via_count * via_cost`` -- the same two
    quantities the A* search prices, measured on the returned geometry rather
    than read out of the search.  ``via_cost`` is the caller's
    ``rules.cost_via`` so the relative wirelength-vs-via weighting matches the
    search that produced the route.

    Args:
        route: A probe result (anything exposing ``.segments`` with ``x1`` /
            ``y1`` / ``x2`` / ``y2`` and ``.vias``).  ``None`` is never passed
            -- callers handle "no path" before costing.
        via_cost: Penalty per via, in the same units as the A* via cost.

    Returns:
        The route's cost.  ``0.0`` for a route with no geometry.
    """
    length = 0.0
    for seg in route.segments:
        length += math.dist((seg.x1, seg.y1), (seg.x2, seg.y2))
    return length + len(route.vias) * via_cost


def trimmed_probe_copper(route: Route, trim_mm: float = 1.0) -> Route:
    """Copy of a probe route with ``trim_mm`` of copper removed from each end.

    The second probe marks the best route under a foreign sentinel net id --
    but the best and second-best routes legitimately *share the pad-approach
    window*: both must reach the same pads, so copper marked all the way into
    the pad halo turns the second search's shared approach into a foreign
    seg-seg clearance violation and the C++ post-route validator rejects every
    candidate (an artifact of the sentinel, not a constraint the real board
    imposes).  Trimming the marked copper back from both pads leaves the
    forbid where it matters -- the mid-corridor the regret actually measures --
    while the shared approach stays routable for both probes.

    Segments are consumed from each end until ``trim_mm`` of path length has
    been removed; the boundary segment is cut, not dropped.  Vias within
    ``trim_mm`` of either original endpoint go with the trimmed copper (an
    in-pad escape via would re-create the shared-pad artifact).  A route
    shorter than ``2 * trim_mm`` trims to empty geometry -- the caller then
    marks nothing and the second probe returns the first path (regret 0):
    a connection that short has no meaningful mid-corridor to lose.

    Args:
        route: The probe route (already carrying the sentinel net ids).
        trim_mm: Length of copper to remove from each end, in mm.  Must clear
            the pad halo (pad half-extent + clearance), hence the 1.0mm
            default for the fleet boards' pad geometries.

    Returns:
        A new Route (input left untouched) with at most
        ``len(route.segments)`` segments and no via inside either trim window.
    """
    from dataclasses import replace as _dc_replace

    segs: list = [_dc_replace(seg) for seg in route.segments]
    if not segs:
        return Route(net=route.net, net_name=route.net_name)
    start_point = (segs[0].x1, segs[0].y1)
    end_point = (segs[-1].x2, segs[-1].y2)

    for end in ("start", "end"):
        remaining = trim_mm
        while remaining > 1e-9 and segs:
            seg = segs[0] if end == "start" else segs[-1]
            length = math.dist((seg.x1, seg.y1), (seg.x2, seg.y2))
            if length <= remaining:
                segs.pop(0) if end == "start" else segs.pop()
                remaining -= length
                continue
            if end == "start":
                ux, uy = (seg.x2 - seg.x1) / length, (seg.y2 - seg.y1) / length
                seg.x1 += ux * remaining
                seg.y1 += uy * remaining
            else:
                ux, uy = (seg.x1 - seg.x2) / length, (seg.y1 - seg.y2) / length
                seg.x2 += ux * remaining
                seg.y2 += uy * remaining
            remaining = 0.0

    kept_vias = [
        via
        for via in route.vias
        if math.dist((via.x, via.y), start_point) > trim_mm
        and math.dist((via.x, via.y), end_point) > trim_mm
    ]
    return Route(net=route.net, net_name=route.net_name, segments=segs, vias=kept_vias)


def regret_order(
    regrets: dict[int, float],
    priority_key: dict[int, tuple],
    class_band: dict[int, int],
) -> list[int]:
    """Order nets biggest-regret-first **within** each net-class priority band.

    Args:
        regrets: Net id -> ``cost(second-best) - cost(best)`` from the two
            empty-board probes.  ``math.inf`` = only one corridor exists
            (second probe found no path) -- that net sorts first in its band.
            ``UNKNOWN_REGRET`` (0.0) = the first probe failed; the net is not
            promoted or demoted on a failed measurement.
        priority_key: Net id -> the router's existing ``_get_net_priority``
            tuple, used as the intra-band tiebreak so the result degrades to
            the default order when all regrets are equal.
        class_band: Net id -> net-class priority (the first element of
            ``_get_net_priority``).  Bands are emitted in ascending order and
            are never interleaved, preserving the clock/diff-pair-first
            invariant.  Pour nets (band 99) stay pinned last without having
            been probed.

    Returns:
        A permutation of ``regrets``' keys.
    """
    return sorted(
        regrets,
        key=lambda net: (
            class_band.get(net, 0),
            -regrets[net],
            priority_key.get(net, ()),
            net,
        ),
    )
