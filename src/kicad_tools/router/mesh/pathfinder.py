"""``MeshPathfinder`` -- the mesh-router single-net route contract (#4268).

Exposes the same "route between two pads" contract as
``CppPathfinder.route`` (``cpp_backend.py:1322``) and returns an ordinary
:class:`~kicad_tools.router.primitives.Route`, so the emission tail and DRC
gate see a normal route object.  The full pipeline:

    inflated pad keep-outs  (ObstacleModel)
      -> poly2tri CDT with holes            (router_cpp.constrained_delaunay)
      -> triangle-dual portal-midpoint A*    (NavMesh.astar)
      -> Simple Stupid Funnel                (string_pull)
      -> clearance-aware 45-degree best-fit  (octilinear_fit)
      -> Route of 45-legal Segments

Single-net only.  Multi-net negotiation, capacity, vias/layer-stitching, and
matched routing are explicitly out of scope for P1 (epic children P2/P3):
routing is confined to the start pad's layer and no vias are emitted.
"""

from __future__ import annotations

from pathlib import Path

from ..primitives import Pad, Route, Segment
from ..rules import DesignRules
from .geometry import Pt
from .navmesh import NavMesh
from .obstacles import ObstacleModel, Rect, rect_contains
from .octilinear import octilinear_fit


class MeshPathfinder:
    """Navmesh single-net pathfinder returning ordinary ``Route`` objects."""

    # Capability flag mirroring ``CppPathfinder.supports_waypoint_injection``
    # (cpp_backend.py:819).  The mesh strategy does its own off-grid handling
    # (pads enter the mesh as exact Steiner vertices), so the grid waypoint /
    # sub-grid escape machinery does not apply.
    supports_waypoint_injection: bool = False

    def __init__(
        self,
        outline: list[Pt],
        pads: list[Pad],
        rules: DesignRules | None = None,
    ) -> None:
        self.outline = outline
        self.pads = pads
        self.rules = rules or DesignRules()
        # Slightly-inset outline bbox used to clamp keep-out holes so poly2tri
        # never sees a hole that pokes through the outer boundary.
        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        self._bbox: Rect = (min(xs), min(ys), max(xs), max(ys))

    # -- construction from a board ----------------------------------------

    @classmethod
    def from_board(
        cls,
        pcb_path_or_text: str | Path,
        rules: DesignRules | None = None,
    ) -> MeshPathfinder:
        """Build a pathfinder from a ``.kicad_pcb`` file or text."""
        from ..io import _extract_edge_segments, load_pads_for_analysis

        if isinstance(pcb_path_or_text, Path):
            text = pcb_path_or_text.read_text()
        elif not pcb_path_or_text.lstrip().startswith("("):
            text = Path(pcb_path_or_text).read_text()
        else:
            text = pcb_path_or_text

        pads = load_pads_for_analysis(text)
        segments = _extract_edge_segments(text)
        outline = _outline_from_edges(segments)
        return cls(outline, pads, rules)

    # -- the route contract -----------------------------------------------

    def route(
        self,
        start: Pad,
        end: Pad,
        net_class: object | None = None,
        **_ignored: object,
    ) -> Route | None:
        """Route a single net between two pads; ``None`` if it cannot.

        ``net_class`` and any negotiation kwargs are accepted for contract
        parity with ``CppPathfinder.route`` but only the trace width is read
        (single-net P1 does no congestion negotiation).
        """
        import kicad_tools.router.router_cpp as router_cpp

        trace_w = getattr(net_class, "trace_width", None) or self.rules.trace_width
        clearance = self.rules.trace_clearance
        agent_radius = trace_w / 2.0 + clearance

        net = start.net
        start_pt: Pt = (start.x, start.y)
        end_pt: Pt = (end.x, end.y)

        # Authoritative obstacle model: EVERY other-net pad, inflated. This is
        # the model the octilinear fit validates every leg against -- a route
        # that cannot clear it is declined (None), never emitted as a short.
        keepouts = self._keepouts(net, agent_radius)
        obstacles = ObstacleModel(self.outline, keepouts)

        # The poly2tri mesh needs its holes disjoint from the Steiner endpoints,
        # so the *mesh* drops any keep-out that swallows an endpoint (a dense
        # pad-escape region).  The authoritative ``obstacles`` above still
        # carries those keep-outs, so the octilinear fit will decline the net
        # if the relaxed corridor cannot actually be cleared.
        mesh_holes = [
            [(r[0], r[1]), (r[2], r[1]), (r[2], r[3]), (r[0], r[3])]
            for r in keepouts
            if not (rect_contains(r, start_pt) or rect_contains(r, end_pt))
        ]

        verts, tris = router_cpp.constrained_delaunay(self.outline, mesh_holes, [start_pt, end_pt])
        if not verts or not tris:
            return None

        navmesh = NavMesh([tuple(v) for v in verts], [tuple(t) for t in tris])
        corridor = navmesh.astar(start_pt, end_pt)
        if corridor is None:
            return None

        geodesic = _pull(navmesh, corridor, start_pt, end_pt)
        fitted = octilinear_fit(geodesic, obstacles.is_clear)
        if fitted is None or len(fitted) < 2:
            return None

        return self._build_route(start, net, trace_w, fitted)

    # -- helpers ----------------------------------------------------------

    def _keepouts(self, net: int, agent_radius: float) -> list[Rect]:
        """Inflated keep-out rects for every OTHER-net pad, clamped + merged."""
        from .obstacles import merge_overlapping

        raw: list[Rect] = []
        bx0, by0, bx1, by1 = self._bbox
        # Keep holes a hair inside the outer boundary (poly2tri requirement).
        margin = 1e-3
        for pad in self.pads:
            if pad.net == net:
                continue  # same-net copper is not an obstacle
            hx = pad.width / 2.0 + agent_radius
            hy = pad.height / 2.0 + agent_radius
            r = (pad.x - hx, pad.y - hy, pad.x + hx, pad.y + hy)
            r = (
                max(r[0], bx0 + margin),
                max(r[1], by0 + margin),
                min(r[2], bx1 - margin),
                min(r[3], by1 - margin),
            )
            if r[2] - r[0] < margin or r[3] - r[1] < margin:
                continue  # clamped away to nothing
            raw.append(r)

        return merge_overlapping(raw)

    def _build_route(self, start: Pad, net: int, trace_w: float, fitted: list[Pt]) -> Route:
        route = Route(net=net, net_name=start.net_name)
        layer = start.layer
        for i in range(len(fitted) - 1):
            a, b = fitted[i], fitted[i + 1]
            route.segments.append(
                Segment(
                    x1=a[0],
                    y1=a[1],
                    x2=b[0],
                    y2=b[1],
                    width=trace_w,
                    layer=layer,
                    net=net,
                    net_name=start.net_name,
                )
            )
        return route


def _pull(navmesh: NavMesh, corridor: list[int], start: Pt, end: Pt) -> list[Pt]:
    from .funnel import string_pull

    return string_pull(navmesh.corridor_to_portals(corridor, start, end))


def _outline_from_edges(
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[Pt]:
    """Board outline as a CCW bbox rectangle from Edge.Cuts segments.

    P1 uses the Edge.Cuts bounding box as the outer boundary.  Every committed
    fleet board in scope is rectangular, so the bbox is the exact outline; a
    concave outline would only over-constrain the free space conservatively.
    """
    if not segments:
        return []
    xs: list[float] = []
    ys: list[float] = []
    for (x1, y1), (x2, y2) in segments:
        xs.extend((x1, x2))
        ys.extend((y1, y2))
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    return [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax)]
