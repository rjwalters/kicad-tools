"""Board geometry and missing links for ``kct board-view`` (issue #6316).

Nothing here imports matplotlib, so the ``list`` view and the tests of the
coordinate frame and the link computation run without it.

Coordinate frames
-----------------

:class:`~kicad_tools.schema.pcb.PCB` hands out footprints, tracks and vias in
the **board-relative** frame (the ``Edge.Cuts`` minimum corner subtracted at
load) but top-level graphics, the board outline included, in the
**sheet-absolute** frame of the file.  Mixing the two draws the copper off the
board, which is how the scratch renderer behind this module first produced a
blank image.  :func:`collect_geometry` converts everything to one frame and
records which one.

Board-relative is the default because it is the frame of ``kct net-status``,
``kct pcb move-footprint --to`` and ``kct pcb strip --region``.  ``kct check``
reports violation locations sheet-absolute, i.e. axis value plus
``pcb.board_origin``; ``absolute=True`` draws in that frame instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.core.geometry import rotate_pad_offset

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "FRAME_BOARD",
    "FRAME_SHEET",
    "BoardGeometry",
    "LinkEnd",
    "LinkReport",
    "MissingLink",
    "PadGeo",
    "Track",
    "ViaGeo",
    "arc_points",
    "collect_geometry",
    "missing_links",
]

FRAME_BOARD = "board-relative"
FRAME_SHEET = "sheet-absolute"

Point = tuple[float, float]
Box = tuple[float, float, float, float]

#: Segments per full turn when a circle or arc is drawn as a polyline.
_ARC_STEPS_PER_TURN = 96


@dataclass(frozen=True)
class PadGeo:
    """One pad, centre and size in the drawing frame."""

    ref: str
    number: str
    net: str
    x: float
    y: float
    width: float
    height: float
    rotation: float  # absolute, degrees (KiCad stores pad angles board-frame)
    shape: str
    layers: frozenset[str]  # copper layers only
    drill: float

    @property
    def name(self) -> str:
        return f"{self.ref}.{self.number}"

    @property
    def half_extents(self) -> tuple[float, float]:
        """Half width and half height of the axis-aligned box round the pad."""
        phi = math.radians(self.rotation)
        c, s = abs(math.cos(phi)), abs(math.sin(phi))
        return (
            (self.width * c + self.height * s) / 2.0,
            (self.width * s + self.height * c) / 2.0,
        )

    def outline(self) -> list[Point]:
        """Pad copper as a closed polygon (round-rect corners drawn square)."""
        hw, hh = self.width / 2.0, self.height / 2.0
        local: list[Point]
        if self.shape == "circle":
            r = min(hw, hh)
            local = [
                (r * math.cos(2 * math.pi * i / 24), r * math.sin(2 * math.pi * i / 24))
                for i in range(24)
            ]
        elif self.shape == "oval":
            local = _stadium(hw, hh)
        else:
            local = [(-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh)]
        out: list[Point] = []
        for lx, ly in local:
            dx, dy = rotate_pad_offset(lx, ly, self.rotation)
            out.append((self.x + dx, self.y + dy))
        return out


@dataclass(frozen=True)
class Track:
    """A straight segment (two points) or a tessellated copper arc."""

    points: tuple[Point, ...]
    width: float
    layer: str
    net: str

    @property
    def start(self) -> Point:
        return self.points[0]

    @property
    def end(self) -> Point:
        return self.points[-1]


@dataclass(frozen=True)
class ViaGeo:
    x: float
    y: float
    size: float
    drill: float
    layers: tuple[str, ...]  # every copper layer the barrel spans
    net: str


@dataclass
class BoardGeometry:
    """Everything the renderer draws, in one named coordinate frame."""

    frame: str
    origin: Point  # pcb.board_origin: sheet position of the board-relative (0, 0)
    copper: list[str]
    pads: list[PadGeo] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[ViaGeo] = field(default_factory=list)
    outline: list[list[Point]] = field(default_factory=list)
    bounds: Box | None = None  # None: no outline and no copper
    outline_error: str = ""

    def net_names(self) -> set[str]:
        names = {p.net for p in self.pads} | {t.net for t in self.tracks}
        names |= {v.net for v in self.vias}
        names.discard("")
        return names


@dataclass(frozen=True)
class LinkEnd:
    name: str  # REF.PAD
    x: float
    y: float


@dataclass(frozen=True)
class MissingLink:
    """One connection a ratsnest would still draw: the closest pad pair
    between two copper islands of the same net."""

    net: str
    a: LinkEnd
    b: LinkEnd

    @property
    def length_mm(self) -> float:
        return math.hypot(self.a.x - self.b.x, self.a.y - self.b.y)

    def to_dict(self) -> dict[str, Any]:
        return {
            "from": self.a.name,
            "to": self.b.name,
            "from_mm": [round(self.a.x, 4), round(self.a.y, 4)],
            "to_mm": [round(self.b.x, 4), round(self.b.y, 4)],
            "length_mm": round(self.length_mm, 3),
        }


@dataclass
class LinkReport:
    """Missing links per unfinished net, plus the pour nets held back.

    ``links`` and ``pour_advisory`` are disjoint: a net appears in exactly one
    of them, or in neither when it is complete.
    """

    links: dict[str, list[MissingLink]] = field(default_factory=dict)
    pour_advisory: list[dict[str, Any]] = field(default_factory=list)

    @property
    def link_count(self) -> int:
        return sum(len(v) for v in self.links.values())

    def links_to_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            net: [link.to_dict() for link in links] for net, links in sorted(self.links.items())
        }


def _stadium(hw: float, hh: float) -> list[Point]:
    """An oval pad: a rectangle with semicircular ends on its short sides."""
    r = min(hw, hh)
    steps = 8
    pts: list[Point] = []
    if hw >= hh:
        cx = hw - r
        for i in range(steps + 1):  # right cap, bottom to top
            a = -math.pi / 2 + math.pi * i / steps
            pts.append((cx + r * math.cos(a), r * math.sin(a)))
        for i in range(steps + 1):  # left cap, top to bottom
            a = math.pi / 2 + math.pi * i / steps
            pts.append((-cx + r * math.cos(a), r * math.sin(a)))
    else:
        cy = hh - r
        for i in range(steps + 1):
            a = math.pi * i / steps
            pts.append((r * math.cos(a), cy + r * math.sin(a)))
        for i in range(steps + 1):
            a = math.pi + math.pi * i / steps
            pts.append((r * math.cos(a), -cy + r * math.sin(a)))
    return pts


def arc_points(start: Point, mid: Point, end: Point) -> list[Point]:
    """Polyline along the circular arc from *start* through *mid* to *end*.

    The first and last points are exactly *start* and *end*.  Collinear input
    (no circle) comes back as the three points unchanged.
    """
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return [start, mid, end]
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    r = math.hypot(x1 - cx, y1 - cy)
    a1 = math.atan2(y1 - cy, x1 - cx)
    a2 = math.atan2(y2 - cy, x2 - cx)
    a3 = math.atan2(y3 - cy, x3 - cx)
    two_pi = 2.0 * math.pi
    # Sweep in whichever direction passes through the mid point.
    sweep = (a3 - a1) % two_pi
    if (a2 - a1) % two_pi > sweep:
        sweep -= two_pi
    steps = max(2, math.ceil(abs(sweep) / two_pi * _ARC_STEPS_PER_TURN))
    pts = [
        (cx + r * math.cos(a1 + sweep * i / steps), cy + r * math.sin(a1 + sweep * i / steps))
        for i in range(steps + 1)
    ]
    pts[0], pts[-1] = start, end
    return pts


def _copper_layers_of(layers: list[str], copper: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for layer in layers:
        if layer == "*.Cu":
            out.update(copper)
        elif layer == "F&B.Cu":
            out.update(name for name in ("F.Cu", "B.Cu") if name in copper)
        elif layer.endswith(".Cu"):
            out.add(layer)
    return frozenset(out)


def _via_span(layers: list[str], copper: list[str]) -> tuple[str, ...]:
    idx = [copper.index(name) for name in layers if name in copper]
    if not idx:
        return tuple(copper)
    return tuple(copper[min(idx) : max(idx) + 1])


def _outline_polylines(pcb: PCB, shift: Point) -> list[list[Point]]:
    """``Edge.Cuts`` graphics as polylines; *shift* is added to every point."""
    sx, sy = shift

    def mv(pt: Point) -> Point:
        return (pt[0] + sx, pt[1] + sy)

    lines: list[list[Point]] = []
    for g in pcb.graphics_on_layer("Edge.Cuts"):
        kind = g.graphic_type
        if kind == "line":
            lines.append([mv(g.start), mv(g.end)])
        elif kind == "rect":
            (x0, y0), (x1, y1) = g.start, g.end
            lines.append([mv(p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))])
        elif kind == "circle":
            cx, cy = g.center if g.center is not None else g.start
            r = math.hypot(g.end[0] - cx, g.end[1] - cy)
            n = _ARC_STEPS_PER_TURN
            lines.append(
                [
                    mv(
                        (
                            cx + r * math.cos(2 * math.pi * i / n),
                            cy + r * math.sin(2 * math.pi * i / n),
                        )
                    )
                    for i in range(n + 1)
                ]
            )
        elif kind == "arc" and g.mid is not None:
            lines.append([mv(p) for p in arc_points(g.start, g.mid, g.end)])
        elif kind == "poly" and g.points:
            lines.append([mv(p) for p in [*g.points, g.points[0]]])
    return lines


def collect_geometry(pcb: PCB, *, absolute: bool = False) -> BoardGeometry:
    """Read pads, tracks, vias and the outline into one coordinate frame."""
    ox, oy = pcb.board_origin
    # PCB attributes are board-relative; add the origin for the sheet frame.
    dx, dy = (ox, oy) if absolute else (0.0, 0.0)
    copper = [layer.name for layer in pcb.copper_layers]
    geo = BoardGeometry(
        frame=FRAME_SHEET if absolute else FRAME_BOARD,
        origin=(ox, oy),
        copper=copper,
        outline_error=getattr(pcb, "outline_error", "") or "",
    )

    for fp in pcb.footprints:
        fx, fy = fp.position
        for pad in fp.pads:
            layers = _copper_layers_of(pad.layers, copper)
            if not layers:
                continue  # paste-only apertures, mask openings
            px, py = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation or 0.0)
            geo.pads.append(
                PadGeo(
                    ref=fp.reference,
                    number=str(pad.number),
                    net=pad.net_name or "",
                    x=fx + px + dx,
                    y=fy + py + dy,
                    width=pad.size[0],
                    height=pad.size[1],
                    rotation=pad.rotation or 0.0,
                    shape=pad.shape,
                    layers=layers,
                    drill=float(pad.drill or 0.0),
                )
            )

    def mv(pt: Point) -> Point:
        return (pt[0] + dx, pt[1] + dy)

    for seg in pcb.segments:
        geo.tracks.append(
            Track((mv(seg.start), mv(seg.end)), seg.width, seg.layer, seg.net_name or "")
        )
    for arc in pcb.arcs:
        pts = tuple(mv(p) for p in arc_points(arc.start, arc.mid, arc.end))
        geo.tracks.append(Track(pts, arc.width, arc.layer, arc.net_name or ""))
    for via in pcb.vias:
        vx, vy = mv(via.position)
        geo.vias.append(
            ViaGeo(vx, vy, via.size, via.drill, _via_span(via.layers, copper), via.net_name or "")
        )

    # Top-level graphics are sheet-absolute, unlike everything above.
    geo.outline = _outline_polylines(pcb, (dx - ox, dy - oy))
    geo.bounds = _bounds(pcb, geo, (dx, dy))
    return geo


def _bounds(pcb: PCB, geo: BoardGeometry, offset: Point) -> Box | None:
    try:
        width, height = pcb.board_size
    except ValueError as exc:  # malformed outline: fall back to the copper
        geo.outline_error = geo.outline_error or str(exc)
        width, height = 0.0, 0.0
    if width > 0 and height > 0:
        return (offset[0], offset[1], offset[0] + width, offset[1] + height)
    xs: list[float] = []
    ys: list[float] = []
    for pad in geo.pads:
        hw, hh = pad.half_extents
        xs += [pad.x - hw, pad.x + hw]
        ys += [pad.y - hh, pad.y + hh]
    for track in geo.tracks:
        xs += [p[0] for p in track.points]
        ys += [p[1] for p in track.points]
    for via in geo.vias:
        xs += [via.x - via.size / 2, via.x + via.size / 2]
        ys += [via.y - via.size / 2, via.y + via.size / 2]
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def missing_links(pcb: PCB, *, absolute: bool = False) -> LinkReport:
    """Per unfinished net, the pad pairs a ratsnest would still draw.

    The islands come from :class:`NetStatusAnalyzer`, the connectivity model
    behind ``kct check`` and ``measure_completion``, which traces zone fill.
    The gate is the one ``ConnectivityRule`` applies: a net that owns filled
    pour copper and is incomplete only by an advisory residual is not an
    error there, so it gets no link here and is listed under
    ``pour_advisory`` instead.  A net therefore has links exactly when
    ``kct check`` reports it, and ``len(links) == open_connections``.

    Each link is the closest pad pair (centre to centre) between the islands
    joined so far and one not yet joined (Prim's algorithm, starting from the
    largest island).
    """
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    ox, oy = pcb.board_origin
    dx, dy = (ox, oy) if absolute else (0.0, 0.0)
    report = LinkReport()

    for status in NetStatusAnalyzer(pcb).analyze().nets:
        if status.total_pads < 2 or status.status == "complete":
            continue
        islands = [
            [LinkEnd(p.full_name, p.position[0] + dx, p.position[1] + dy) for p in island]
            for island in status.islands
            if island
        ]
        if len(islands) < 2:
            continue
        if status.has_filled_zone and status.is_advisory_incomplete:
            report.pour_advisory.append(
                {
                    "net": status.net_name,
                    "open_connections": status.open_connections,
                    "stranded_pads": sorted(end.name for island in islands[1:] for end in island),
                    "zone_layers": list(status.plane_layers),
                }
            )
            continue
        report.links[status.net_name] = _spanning_links(status.net_name, islands)

    report.pour_advisory.sort(key=lambda entry: entry["net"])
    return report


def _spanning_links(net: str, islands: list[list[LinkEnd]]) -> list[MissingLink]:
    joined: list[LinkEnd] = list(islands[0])
    todo = islands[1:]
    links: list[MissingLink] = []
    while todo:
        best: tuple[float, LinkEnd, LinkEnd, int] | None = None
        for index, island in enumerate(todo):
            for a in joined:
                for b in island:
                    dist = math.hypot(a.x - b.x, a.y - b.y)
                    if best is None or dist < best[0]:
                        best = (dist, a, b, index)
        assert best is not None
        links.append(MissingLink(net, best[1], best[2]))
        joined.extend(todo.pop(best[3]))
    return links
