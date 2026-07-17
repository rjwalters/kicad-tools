"""Per-layer lattice masking + committed-copper model (issue #4278).

Two legality sources gate every lattice resource, mirroring the mesh
``ObstacleModel`` discipline (``router/mesh/obstacles.py``) extended
per copper layer:

* **Static pad masks** (:class:`LatticeObstacleModel`) -- built ONCE with
  the lattice.  Every lattice node/edge records which inflated pad
  keep-outs cover it, per layer: an SMD pad masks only its own copper
  layer, a through-hole pad masks EVERY layer.  Same-net pads are not
  obstacles, so the masks store pad *indices* and the per-net predicate
  resolves net membership at query time (no per-net rebuild).

* **Committed copper** (:class:`CommittedCopper`) -- the dynamic model a
  negotiation pass accumulates.  Checks are geometric (segment-segment /
  segment-point distance against real gaps), not merely discrete
  node-occupancy: adjacent fine-lattice rows can sit closer than the
  copper gap, so occupancy alone could ship a clearance violation.  This
  is the #3906 "consult the obstacle model, never blind-fit" lesson.
"""

from __future__ import annotations

from collections import defaultdict

from ..primitives import Pad
from .geometry import (
    Pt,
    Rect,
    SegHash,
    dist,
    pt_in_rect,
    seg_pt_dist,
    seg_rect_intersect,
    seg_seg_dist,
)
from .quadtree import EdgeKey, NodeKey, OctilinearLattice

_BUCKET = 4.0  # mm; pad-lookup acceleration grid


class LatticeObstacleModel:
    """Static per-layer pad masks over a built lattice (built once per board).

    Args:
        lattice: the built :class:`OctilinearLattice`.
        pads: every pad on the board.
        pad_layer_indices: for each pad, the routing-layer indices its copper
            occupies (SMD: its own layer; PTH: all layers) -- resolved by the
            caller against the real :class:`~kicad_tools.router.layers.LayerStack`.
        num_layers: copper layer count of the stack.
        agent_radius: half trace width + clearance (Minkowski inflation).
    """

    def __init__(
        self,
        lattice: OctilinearLattice,
        pads: list[Pad],
        pad_layer_indices: list[tuple[int, ...]],
        num_layers: int,
        agent_radius: float,
    ) -> None:
        self.lattice = lattice
        self.pads = pads
        self.pad_layer_indices = pad_layer_indices
        self.num_layers = num_layers
        self.agent_radius = agent_radius

        # Inflated keep-out rectangle per pad (pad half-extent + agent radius).
        self.pad_rects: list[Rect] = []
        for pad in pads:
            hx = pad.width / 2.0 + agent_radius
            hy = pad.height / 2.0 + agent_radius
            self.pad_rects.append((pad.x - hx, pad.y - hy, pad.x + hx, pad.y + hy))

        # Pad-lookup buckets.
        self._pad_buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
        for idx, rect in enumerate(self.pad_rects):
            for ix in range(int(rect[0] // _BUCKET), int(rect[2] // _BUCKET) + 1):
                for iy in range(int(rect[1] // _BUCKET), int(rect[3] // _BUCKET) + 1):
                    self._pad_buckets[(ix, iy)].append(idx)

        # node_pads[L][key] / edge_pads[L][edge] -> pad indices whose keep-out
        # covers that resource on layer L.
        self.node_pads: list[dict[NodeKey, list[int]]] = [{} for _ in range(num_layers)]
        self.edge_pads: list[dict[EdgeKey, list[int]]] = [{} for _ in range(num_layers)]
        for key, point in lattice.nodes.items():
            for idx in self.pads_near(point[0], point[1], point[0], point[1]):
                if pt_in_rect(point, self.pad_rects[idx]):
                    for layer in pad_layer_indices[idx]:
                        self.node_pads[layer].setdefault(key, []).append(idx)
        for edge in lattice.edges:
            a = lattice.node_point(edge[0])
            b = lattice.node_point(edge[1])
            x0, x1 = min(a[0], b[0]), max(a[0], b[0])
            y0, y1 = min(a[1], b[1]), max(a[1], b[1])
            for idx in self.pads_near(x0, y0, x1, y1):
                if seg_rect_intersect(a, b, self.pad_rects[idx]):
                    for layer in pad_layer_indices[idx]:
                        self.edge_pads[layer].setdefault(edge, []).append(idx)

    # -- queries -------------------------------------------------------------

    def pads_near(self, x0: float, y0: float, x1: float, y1: float) -> set[int]:
        """Pad indices whose inflated rect may touch the query box."""
        out: set[int] = set()
        for ix in range(int(x0 // _BUCKET), int(x1 // _BUCKET) + 1):
            for iy in range(int(y0 // _BUCKET), int(y1 // _BUCKET) + 1):
                out.update(self._pad_buckets.get((ix, iy), ()))
        return out

    def node_blocked(self, key: NodeKey, layer: int, net: int) -> bool:
        """True if the node sits in an OTHER-net pad keep-out on ``layer``."""
        return any(self.pads[idx].net != net for idx in self.node_pads[layer].get(key, ()))

    def edge_blocked(self, edge: EdgeKey, layer: int, net: int) -> bool:
        """True if the edge crosses an OTHER-net pad keep-out on ``layer``."""
        return any(self.pads[idx].net != net for idx in self.edge_pads[layer].get(edge, ()))

    def segment_blocked(self, a: Pt, b: Pt, layer: int, net: int, extra: float = 0.0) -> bool:
        """True if free segment ``a-b`` (e.g. a stub leg) enters an other-net
        pad keep-out on ``layer`` (geometric, not lattice-resource, check).

        ``extra`` (issue #4271) inflates every pad keep-out beyond the
        global ``agent_radius`` -- the per-connection surcharge for a net
        class wider (or more cleared) than the board default, so oversize
        copper is spaced from other-net pads at its TRUE half-width.  The
        degenerate ``a == b`` form doubles as a point (lattice-node) probe.
        """
        x0, x1 = min(a[0], b[0]), max(a[0], b[0])
        y0, y1 = min(a[1], b[1]), max(a[1], b[1])
        for idx in self.pads_near(x0 - extra, y0 - extra, x1 + extra, y1 + extra):
            if self.pads[idx].net == net:
                continue
            if layer not in self.pad_layer_indices[idx]:
                continue
            rect = self.pad_rects[idx]
            if extra > 0.0:
                rect = (rect[0] - extra, rect[1] - extra, rect[2] + extra, rect[3] + extra)
            if seg_rect_intersect(a, b, rect):
                return True
        return False


class CommittedCopper:
    """Copper committed by already-routed nets in one negotiation pass.

    Geometric per-layer model: traces live in per-layer :class:`SegHash`
    structures, vias in a flat list (a through-via blocks EVERY layer).
    All clearance predicates are centreline distances against the real
    gaps supplied by the pathfinder.

    Issue #4271 (net-class widths): every committed segment carries its TRUE
    ``half_width`` and its class ``clearance``, and every predicate takes the
    querying connection's own ``half`` / ``clearance``.  The required
    centreline gap between two traces is::

        own_half + stored_half + max(own_clearance, stored_clearance)

    so 2.6 mm HV_HICUR copper is spaced as 2.6 mm copper and a 0.3 mm HV
    class clearance is honored from EITHER side of the pair (mirroring
    KiCad's conditional-rule semantics, where the wider constraint governs
    when either net is in the class).  ``None`` arguments fall back to the
    board-global trace geometry -- the pre-#4271 behavior, byte-for-byte.
    """

    def __init__(
        self,
        num_layers: int,
        *,
        trace_half: float,
        clearance: float,
        via_radius: float,
        via_via_gap: float,
        same_net_via_gap: float,
    ) -> None:
        self.num_layers = num_layers
        self.trace_half = trace_half  # global default copper half-width
        self.clearance = clearance  # global default (floor) clearance
        self.via_radius = via_radius  # via body radius
        self.via_via_gap = via_via_gap  # via centre to via centre (cross-net)
        self.same_net_via_gap = same_net_via_gap  # hole-to-hole floor
        self.copper: list[SegHash] = [SegHash() for _ in range(num_layers)]
        self.vias: list[tuple[Pt, int]] = []

    # -- mutation --------------------------------------------------------

    def add_run(
        self,
        layer: int,
        points: list[Pt],
        net: int,
        half_width: float,
        clearance: float | None = None,
    ) -> None:
        """Commit a polyline of copper on ``layer`` at its TRUE half-width
        and class clearance (``None`` -> the board-global clearance)."""
        clr = self.clearance if clearance is None else clearance
        for a, b in zip(points, points[1:], strict=False):
            if dist(a, b) > 1e-9:
                self.copper[layer].add(a, b, net, half_width, clr)

    def add_via(self, point: Pt, net: int) -> None:
        """Commit a through-via (blocks the site on ALL layers)."""
        self.vias.append((point, net))

    # -- predicates --------------------------------------------------------

    def _own(self, half: float | None, clearance: float | None) -> tuple[float, float]:
        """Resolve the querying connection's (half-width, clearance)."""
        own_half = self.trace_half if half is None else half
        own_clr = self.clearance if clearance is None else clearance
        return own_half, own_clr

    def seg_clear(
        self,
        a: Pt,
        b: Pt,
        layer: int,
        net: int,
        half: float | None = None,
        clearance: float | None = None,
    ) -> bool:
        """True if segment ``a-b`` on ``layer`` clears other-net copper + vias."""
        own_half, own_clr = self._own(half, clearance)
        pad = own_half + own_clr + 0.5
        for c, d, cnet, hw, iclr in self.copper[layer].query_seg(a, b, pad=pad):
            gap = own_half + hw + max(own_clr, iclr)
            if cnet != net and seg_seg_dist(a, b, c, d) < gap - 1e-9:
                return False
        via_gap = self.via_radius + own_clr + own_half
        for point, vnet in self.vias:
            if vnet != net and seg_pt_dist(a, b, point) < via_gap - 1e-9:
                return False
        return True

    def node_clear(
        self,
        point: Pt,
        layer: int,
        net: int,
        half: float | None = None,
        clearance: float | None = None,
    ) -> bool:
        """True if a node site on ``layer`` clears other-net copper + vias."""
        own_half, own_clr = self._own(half, clearance)
        pad = own_half + own_clr + 0.5
        for c, d, cnet, hw, iclr in self.copper[layer].query_seg(point, point, pad=pad):
            gap = own_half + hw + max(own_clr, iclr)
            if cnet != net and seg_pt_dist(c, d, point) < gap - 1e-9:
                return False
        via_gap = self.via_radius + own_clr + own_half
        for vpt, vnet in self.vias:
            if vnet != net and dist(point, vpt) < via_gap - 1e-9:
                return False
        return True

    def via_clear(self, point: Pt, net: int) -> bool:
        """True if a through-via at ``point`` clears committed copper (ALL
        layers) and committed vias (cross-net body gap, same-net hole gap).

        The via-to-trace gap honors each stored segment's TRUE half-width
        and class clearance (#4271): ``via_radius + stored_half +
        max(global_clearance, stored_clearance)``.
        """
        pad = self.via_radius + self.clearance + self.trace_half + 2.0
        for layer in range(self.num_layers):
            for c, d, cnet, hw, iclr in self.copper[layer].query_seg(point, point, pad=pad):
                gap = self.via_radius + hw + max(self.clearance, iclr)
                if cnet != net and seg_pt_dist(c, d, point) < gap - 1e-9:
                    return False
        for vpt, vnet in self.vias:
            # Cross-net vias must honor BOTH the copper gap (via_via_gap =
            # via diameter + clearance, centre-to-centre) AND the drill
            # hole-to-hole floor (same_net_via_gap = via_drill +
            # min_hole_to_hole).  At JLC defaults the copper gap alone puts
            # two 0.3mm drills only 0.45mm hole-edge-to-edge -- under the
            # 0.5mm floor (issue #4291: 16 hole_to_hole DRC warnings on the
            # softstart P4 run of record).
            if vnet != net:
                gap = max(self.via_via_gap, self.same_net_via_gap)
            else:
                gap = self.same_net_via_gap
            if dist(point, vpt) < gap - 1e-9:
                return False
        return True
