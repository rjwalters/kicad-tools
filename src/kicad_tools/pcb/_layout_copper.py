"""Copper checks the layout -> board bridge runs before it writes anything.

Issue #7022 (review).  :class:`~kicad_tools.pcb.exporter.KiCadPCBExporter`
turns block geometry into real copper on real nets, so a block trace that
ends beside its pad, or a straight inter-block line drawn across a foreign
pad, used to become a ``shorting_items`` / ``tracks_crossing`` DRC error with a
clean report.  This module measures every new segment and via against the
board's **real** pad shapes (at the positions the placements are about to
give them), the board's existing copper and the other new copper:

* each segment endpoint must land inside a pad, or on a track or via, of its
  own net (a new one or one already on the board);
* no segment or via may come closer than the board's clearance rule to a pad,
  track or via on a different net (unconnected pads count as a different net).

Pads are modelled as their bounding rectangle (``rect`` / ``roundrect`` /
``trapezoid`` / ``custom``), a circle or a capsule (``oval``).  For a
roundrect that is slightly conservative at the corners, which errs towards
rejecting.  Zones are not checked: they are refilled around new copper.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from kicad_tools.core.geometry import (
    point_to_segment_distance,
    rotate_pad_offset,
    segment_to_segment_distance,
)

if TYPE_CHECKING:
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules
    from kicad_tools.schema.pcb import PCB

_EPS = 1e-6
XY = tuple[float, float]


@dataclass(frozen=True)
class PadShape:
    """One pad, in board-relative coordinates, after the pending moves."""

    ref: str
    number: str
    net: str
    center: XY
    rotation: float  # absolute, KiCad convention
    shape: str
    size: XY
    layers: frozenset[str]
    footprint_layer: str
    pad_type: str

    @property
    def label(self) -> str:
        return f"{self.ref}.{self.number}"

    def _local(self, x: float, y: float) -> XY:
        # Inverse of rotate_pad_offset(local, rotation).
        return rotate_pad_offset(x - self.center[0], y - self.center[1], -self.rotation)

    def distance_to_point(self, p: XY) -> float:
        """Distance from ``p`` to the pad shape (0 inside)."""
        return self.distance_to_segment(p, p)

    def distance_to_segment(self, a: XY, b: XY) -> float:
        """Distance from segment ``a``-``b`` (a centreline) to the pad shape."""
        ax, ay = self._local(*a)
        bx, by = self._local(*b)
        w, h = self.size
        if self.shape == "circle":
            return max(0.0, point_to_segment_distance(0.0, 0.0, ax, ay, bx, by) - w / 2)
        if self.shape == "oval":
            # A capsule: a core segment along the long axis, radius half the short side.
            if w >= h:
                half, r = (w - h) / 2, h / 2
                core = ((-half, 0.0), (half, 0.0))
            else:
                half, r = (h - w) / 2, w / 2
                core = ((0.0, -half), (0.0, half))
            d = segment_to_segment_distance(ax, ay, bx, by, *core[0], *core[1])
            return max(0.0, d - r)
        return _segment_box_distance((ax, ay), (bx, by), w / 2, h / 2)


def _segment_box_distance(a: XY, b: XY, hw: float, hh: float) -> float:
    """Distance from segment ``a``-``b`` to the axis-aligned box ``|x|<=hw, |y|<=hh``."""

    def point_box(p: XY) -> float:
        dx = max(abs(p[0]) - hw, 0.0)
        dy = max(abs(p[1]) - hh, 0.0)
        return math.hypot(dx, dy)

    da, db = point_box(a), point_box(b)
    if da == 0.0 or db == 0.0:
        return 0.0
    corners = ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))
    best = min(da, db)
    for i, c in enumerate(corners):
        d = corners[(i + 1) % 4]
        best = min(
            best, segment_to_segment_distance(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1])
        )
    return best


@dataclass
class CopperItem:
    """A new segment or via the bridge wants to write (board-relative)."""

    index: int
    kind: str  # "trace" | "via"
    where: str  # human-readable, sheet-absolute
    net: str
    layers: frozenset[str]  # a trace's one layer; every copper layer a via spans
    start: XY
    end: XY  # == start for a via
    width: float  # a via's diameter
    reasons: list[str] = field(default_factory=list)

    @property
    def layer(self) -> str:
        return next(iter(self.layers))


def via_layers(pair: Sequence[str], copper: list[str]) -> frozenset[str]:
    """Every copper layer a via between ``pair`` passes through."""
    try:
        i, j = sorted((copper.index(pair[0]), copper.index(pair[1])))
    except ValueError:
        return frozenset(pair)
    return frozenset(copper[i : j + 1])


def _pad_copper(layers: Iterable[str], copper: list[str]) -> frozenset[str]:
    out: set[str] = set()
    for layer in layers:
        if layer == "*.Cu":
            out.update(copper)
        elif layer == "F&B.Cu":
            out.update(name for name in ("F.Cu", "B.Cu") if name in copper)
        elif layer in copper:
            out.add(layer)
    return frozenset(out)


def board_pads(
    pcb: PCB,
    moves: dict[str, tuple[float, float, float]],
    copper: list[str],
) -> list[PadShape]:
    """Every copper pad on the board, with ``moves`` (``ref -> (x, y, rot)``) applied."""
    pads: list[PadShape] = []
    for fp in pcb.footprints:
        x, y, rot = moves.get(fp.reference, (fp.position[0], fp.position[1], fp.rotation))
        for pad in fp.pads:
            layers = _pad_copper(pad.layers, copper)
            if not layers:
                continue
            ox, oy = rotate_pad_offset(pad.position[0], pad.position[1], rot)
            pads.append(
                PadShape(
                    ref=fp.reference,
                    number=pad.number,
                    net=pad.net_name or "",
                    center=(x + ox, y + oy),
                    rotation=pad.rotation - fp.rotation + rot,
                    shape=pad.shape,
                    size=(float(pad.size[0]), float(pad.size[1])),
                    layers=layers,
                    footprint_layer=fp.layer,
                    pad_type=pad.type,
                )
            )
    return pads


def _fmt(xy: XY, origin: XY) -> str:
    return f"({xy[0] + origin[0]:g}, {xy[1] + origin[1]:g})"


def _net(name: str) -> str:
    return repr(name) if name else "<no net>"


def check_copper(
    pcb: PCB,
    items: list[CopperItem],
    pads: list[PadShape],
    rules: BoardClearanceRules,
    copper: list[str],
    origin: XY,
) -> None:
    """Fill each item's ``reasons``; an item with reasons must not be written.

    Lenient callers drop the failing items, so endpoints that only touched a
    dropped item are re-checked until nothing changes.
    """
    from kicad_tools.router.board_clearance_rules import ItemProps

    def props(kind: str, net: str, layer: str | None = None, pad: PadShape | None = None):
        if pad is not None:
            return ItemProps("Pad", net, pad.pad_type != "np_thru_hole", None, pad.footprint_layer)
        return ItemProps("Via" if kind == "via" else "Track", net, True, None, layer)

    def required(a: ItemProps, b: ItemProps, layer: str | None) -> float:
        return max(rules.clearance(a, b, layer), rules.physical_clearance(a, b, layer))

    existing_tracks = [
        (s.net_name or "", s.layer, s.start, s.end, s.width) for s in pcb.segments
    ]  # fmt: skip
    existing_vias = [
        (v.net_name or "", via_layers(v.layers, copper), v.position, v.size) for v in pcb.vias
    ]

    # -- foreign copper (independent of which other items survive) ---------
    for item in items:
        half = item.width / 2
        for pad in pads:
            shared = item.layers & pad.layers
            if not shared or pad.net == item.net:
                continue
            gap = pad.distance_to_segment(item.start, item.end) - half
            layer = item.layer if item.kind == "trace" else None
            need = required(
                props(item.kind, item.net, layer), props("pad", pad.net, pad=pad), layer
            )
            if gap < need - _EPS:
                item.reasons.append(_clash("pad " + pad.label, pad.net, gap, need))
        for net, layer, s, e, w in existing_tracks:
            if net == item.net or layer not in item.layers:
                continue
            gap = segment_to_segment_distance(*item.start, *item.end, *s, *e) - half - w / 2
            need = required(props(item.kind, item.net, layer), props("trace", net, layer), layer)
            if gap < need - _EPS:
                item.reasons.append(_clash("an existing track", net, gap, need))
        for net, layers, pos, size in existing_vias:
            if net == item.net or not (layers & item.layers):
                continue
            gap = point_to_segment_distance(*pos, *item.start, *item.end) - half - size / 2
            need = required(props(item.kind, item.net), props("via", net), None)
            if gap < need - _EPS:
                item.reasons.append(_clash("an existing via", net, gap, need))
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.net == b.net or not (a.layers & b.layers):
                continue
            gap = (
                segment_to_segment_distance(*a.start, *a.end, *b.start, *b.end)
                - a.width / 2
                - b.width / 2
            )
            layer = a.layer if a.kind == b.kind == "trace" else None
            need = required(props(a.kind, a.net, layer), props(b.kind, b.net, layer), layer)
            if gap < need - _EPS:
                a.reasons.append(_clash(f"new {b.kind} {b.where}", b.net, gap, need))
                b.reasons.append(_clash(f"new {a.kind} {a.where}", a.net, gap, need))

    # -- endpoint contact (iterate: a dropped item can strand its neighbours) --
    def anchored(item: CopperItem, p: XY, alive: list[CopperItem]) -> bool:
        layer = item.layer
        for pad in pads:
            if pad.net == item.net and layer in pad.layers and pad.distance_to_point(p) <= _EPS:
                return True
        for net, seg_layer, s, e, w in existing_tracks:
            if net == item.net and seg_layer == layer:
                if point_to_segment_distance(*p, *s, *e) <= w / 2 + _EPS:
                    return True
        for net, layers, pos, size in existing_vias:
            if net == item.net and layer in layers and math.dist(p, pos) <= size / 2 + _EPS:
                return True
        for other in alive:
            if other is item or other.net != item.net or layer not in other.layers:
                continue
            if point_to_segment_distance(*p, *other.start, *other.end) <= other.width / 2 + _EPS:
                return True
        return False

    traces = [item for item in items if item.kind == "trace"]
    stranded: dict[int, list[str]] = {}
    while True:
        alive = [item for item in items if not item.reasons and item.index not in stranded]
        changed = False
        for item in traces:
            if item.reasons or item.index in stranded:
                continue
            loose = [p for p in (item.start, item.end) if not anchored(item, p, alive)]
            if loose:
                stranded[item.index] = [
                    f"end {_fmt(p, origin)} touches no pad, track or via on net "
                    f"{_net(item.net)} (an open end)"
                    for p in dict.fromkeys(loose)
                ]
                changed = True
        if not changed:
            break
    for item in traces:
        item.reasons.extend(stranded.get(item.index, []))


def _clash(what: str, net: str, gap: float, need: float) -> str:
    if gap <= _EPS:
        return f"shorts {what} on net {_net(net)}"
    return f"is {gap:.3f} mm from {what} on net {_net(net)} (clearance {need:g} mm)"


__all__: list[str] = []
