"""Soft partner-corridor cost for uncoupled diff-pair fallback legs (#6703).

When a diff pair's coupled (fat-centreline) search declines and the pair
falls back to two single-ended legs (#6373), each leg used to be routed as an
unrelated net.  Wherever an obstacle wall forces a detour, the two legs then
picked their own cheapest way around it -- different sides of the wall,
different via counts -- and the pair's length skew grew by several mm (board
07rf TMDS_D0/D1: 0.6/1.6 mm -> 7.2/8.4 mm after #6673's ``+1V8`` pour-link
keep-outs walled U4's ball field on F.Cu).

:class:`PartnerGuide` is the fix's cost term: once the first leg is routed,
the second leg's A* pays a soft surcharge on every lattice edge that strays
further than ``near`` from the first leg's committed copper **on the same
layer**.  Staying beside the partner is free; straying, or running on a layer
the partner does not use there, costs up to ``weight * cap`` per mm.  The term
only ever *adds* cost, so the A* distance heuristic stays admissible, and it
is soft -- a leg with no symmetric path still routes, just less matched.
"""

from __future__ import annotations

from .geometry import Pt, SegHash, dist, seg_pt_dist


class PartnerGuide:
    """Per-layer index of a routed partner leg, scored as a corridor cost.

    Args:
        runs: The partner leg's ``(layer, points, widths)`` runs, exactly as
            the lattice search emits them (``_RouteResult.runs``).
        near: Distance (mm, edge midpoint to partner centreline) within which
            following the partner costs nothing -- about two pair pitches, so
            the leg may sit one lattice line over from its partner.
        cap: Excess distance (mm) beyond ``near`` at which the surcharge
            saturates.  Also the charge for a layer the partner never uses
            nearby.  Bounded so a leg that genuinely cannot follow its partner
            is not pushed into an absurd detour.
        weight: Surcharge per mm of edge length per mm of excess distance.
    """

    def __init__(
        self,
        runs: list[tuple[int, list[Pt], list[float]]],
        *,
        near: float,
        cap: float,
        weight: float,
    ) -> None:
        self.near = near
        self.cap = cap
        self.weight = weight
        self.layers: dict[int, SegHash] = {}
        for layer, points, _widths in runs:
            index = self.layers.setdefault(layer, SegHash())
            for a, b in zip(points, points[1:], strict=False):
                if dist(a, b) > 1e-9:
                    index.add(a, b, 0, 0.0, 0.0)
        self._memo: dict[tuple[Pt, Pt, int], float] = {}

    def excess(self, a: Pt, b: Pt, layer: int) -> float:
        """Distance (mm) beyond ``near`` from the partner, clamped to ``cap``."""
        key = (a, b, layer)
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0)
        best = self.near + self.cap
        index = self.layers.get(layer)
        if index is not None:
            for c, d, _net, _half, _clr in index.query_seg(mid, mid, pad=best):
                best = min(best, seg_pt_dist(c, d, mid))
        value = min(max(0.0, best - self.near), self.cap)
        self._memo[key] = value
        return value

    def edge_cost(self, a: Pt, b: Pt, layer: int) -> float:
        """Soft surcharge for routing lattice edge ``a-b`` on ``layer``."""
        return self.weight * dist(a, b) * self.excess(a, b, layer)
