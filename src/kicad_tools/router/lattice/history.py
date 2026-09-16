"""Soft negotiation demand over emitted copper, including escape legs."""

from .geometry import Pt, SegHash, dist, seg_seg_dist
from .pairwise import LatticePairwise


class PhysicalHistory(dict[tuple[object, ...], float]):
    """Retain discrete history and index the physical corridors it represents."""

    def __init__(self) -> None:
        super().__init__()
        self.layers: dict[int, SegHash] = {}
        self.demands: list[tuple[int, float]] = []

    def add_segment(
        self, a: Pt, b: Pt, layer: int, net: int, half: float, clearance: float, weight: float
    ) -> None:
        index = len(self.demands)
        self.demands.append((net, weight))
        self.layers.setdefault(layer, SegHash()).add(a, b, index, half, clearance)

    def cost(
        self,
        a: Pt,
        b: Pt,
        layer: int,
        net: int,
        half: float,
        clearance: float,
        pairwise: LatticePairwise | None = None,
        partner_net: int | None = None,
    ) -> float:
        copper = self.layers.get(layer)
        if copper is None:
            return 0.0
        # Count each demanding net once, independently of how finely its
        # desired route is segmented or how often its corridor was captured.
        weights: dict[int, float] = {}
        seen: set[int] = set()
        for c, d, index, other_half, other_clearance in copper.query_seg(
            a, b, pad=half + max(clearance, pairwise.max_required_for(net) if pairwise else 0.0)
        ):
            if index in seen:
                continue
            seen.add(index)
            other_net, weight = self.demands[index]
            if other_net in (net, partner_net) or weights.get(other_net, 0.0) >= weight:
                continue
            required = max(clearance, other_clearance)
            if pairwise is not None and not pairwise.exempt_seg_seg(
                a, b, c, d, net, other_net, layer
            ):
                required = max(required, pairwise.required(net, other_net))
            gap = half + other_half + required
            if seg_seg_dist(a, b, c, d) < gap - 1e-9:
                weights[other_net] = max(weights.get(other_net, 0.0), weight)
        return max(dist(a, b), 0.1) * sum(weights.values())
