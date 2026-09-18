"""Bounded via-hop bridge planning between disjoint same-net copper components.

The board recipes' pour-connectivity repair (``_repair_pour_connectivity``)
ends with a *via-hop* stage: when two components of one net share a layer but
the direct same-layer line is blocked by foreign copper, it drops a via inside
each component near the gap and crosses the blockage on another layer.

That stage assumed **both** endpoints still need a new via.  That assumption is
unsatisfiable whenever the copper element nearest the gap is *already* a via
barrel of the same net (issue #5507, Board07 ``U4.C2`` on ``+1V2``):

* a barrel's own-net copper footprint is its annulus, so every retreat point
  the stage tries either falls outside that small disc (the "must land inside
  the component" guard rejects it) or falls inside it -- where the fab
  drill-to-drill floor rejects a second drill.  Both outcomes reject the
  candidate, for every retreat, on every layer;
* the stage therefore skipped the pair entirely and the component stayed
  open, even though the destination barrel already spans the copper stack and
  needs no new via at all.

The correction here is to treat an existing barrel as a **reusable endpoint**:
its centre is a legal bridge terminus that costs no new drill.  A bridge to a
barrel therefore needs one new via (or zero, when both sides are barrels)
instead of two, and the impossible second drill is never attempted.

The planner is deliberately free of any clearance model of its own: the caller
supplies its existing ``via_ok`` / ``path_ok`` predicates, so the authored
manufacturing rules that already govern the repair remain the only physical
authority.  Nothing here mutates the geometry it is given -- a rejected
candidate leaves the caller's obstacle index untouched, and the caller commits
copper only after a complete plan is returned.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from kicad_tools._shapely import require_shapely

#: Element kind (third slot of the recipes' ``own`` tuples) that marks an
#: existing via barrel -- the one kind that is reusable as-is.
VIA_KIND = "via"

Point2D = tuple[float, float]


@dataclass(frozen=True)
class BridgeSide:
    """One copper element a bridge can terminate on.

    Attributes:
        geometry: Shapely geometry of the element's own-net copper.
        layers: Copper layers the element actually reaches.
        kind: Element kind; :data:`VIA_KIND` marks an existing barrel.
    """

    geometry: Any
    layers: frozenset[str]
    kind: str


@dataclass(frozen=True)
class BridgeEndpoint:
    """A validated bridge terminus.

    ``new_via`` is ``False`` when an existing barrel is being reused, in
    which case ``point`` is that barrel's centre and the caller must not
    emit a drill there.
    """

    point: Point2D
    layers: frozenset[str]
    new_via: bool


@dataclass(frozen=True)
class BridgePlan:
    """A complete, fully validated via-hop bridge."""

    source: BridgeEndpoint
    destination: BridgeEndpoint
    layer: str

    @property
    def new_vias(self) -> tuple[Point2D, ...]:
        """Positions where the caller must emit a new via, in emit order."""
        return tuple(
            endpoint.point for endpoint in (self.source, self.destination) if endpoint.new_via
        )


def _endpoints(
    side: BridgeSide,
    anchor: Point2D,
    direction: Point2D,
    retreats: Sequence[float],
    via_ok: Callable[[Point2D], bool],
    new_via_layers: frozenset[str],
) -> Iterator[BridgeEndpoint]:
    """Yield legal termini on ``side``, nearest-first.

    An existing barrel yields exactly one candidate -- itself.  Any other
    element yields the retreat points that both land on its own copper and
    satisfy the caller's via predicate, in the caller's retreat order.
    """
    from shapely.geometry import Point  # type: ignore[import-untyped]

    if side.kind == VIA_KIND:
        centre = side.geometry.centroid
        yield BridgeEndpoint((centre.x, centre.y), side.layers, new_via=False)
        return
    for back in retreats:
        candidate = (anchor[0] + direction[0] * back, anchor[1] + direction[1] * back)
        if not side.geometry.intersects(Point(candidate)):
            continue
        if not via_ok(candidate):
            continue
        yield BridgeEndpoint(candidate, new_via_layers, new_via=True)


def plan_via_hop_bridge(
    source: BridgeSide,
    destination: BridgeSide,
    *,
    layers: Sequence[str],
    retreats: Sequence[float],
    via_ok: Callable[[Point2D], bool],
    path_ok: Callable[[Point2D, Point2D, str], bool],
    new_via_layers: frozenset[str],
) -> BridgePlan | None:
    """Plan one bounded via-hop bridge from ``source`` to ``destination``.

    Candidates are enumerated in the caller's own order -- source retreats
    outermost, destination retreats next, ``layers`` innermost -- and the
    first combination whose new drills pass ``via_ok`` and whose chord passes
    ``path_ok`` is returned.  An existing same-net barrel on either side is
    reused in place instead of being drilled through a second time.

    Args:
        source: Element on the component being reconnected.
        destination: Element on the component it must join.
        layers: Candidate crossing layers, in preference order.
        retreats: Distances to back a *new* via away from the gap, nearest
            first.
        via_ok: Predicate for a new drill at a point; never consulted for a
            reused barrel, which needs no drill.
        path_ok: Predicate for the straight chord between two termini on a
            layer.
        new_via_layers: Layers a newly emitted via reaches (a through via
            spans the whole stack).

    Returns:
        The first complete plan, or ``None`` when no candidate clears.  The
        input geometry is never modified.
    """
    require_shapely("plan a pour-connectivity via-hop bridge")
    from shapely.ops import nearest_points  # type: ignore[import-untyped]

    pa, pb = nearest_points(source.geometry, destination.geometry)
    vec = (pb.x - pa.x, pb.y - pa.y)
    norm = math.hypot(*vec) or 1.0
    ux, uy = vec[0] / norm, vec[1] / norm
    for start in _endpoints(source, (pa.x, pa.y), (-ux, -uy), retreats, via_ok, new_via_layers):
        for end in _endpoints(
            destination, (pb.x, pb.y), (ux, uy), retreats, via_ok, new_via_layers
        ):
            for layer in layers:
                if layer not in start.layers or layer not in end.layers:
                    continue
                if not path_ok(start.point, end.point, layer):
                    continue
                return BridgePlan(start, end, layer)
    return None
