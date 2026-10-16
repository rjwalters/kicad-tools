"""KiCad-exact net length: the one measurement shared by checker and tuners.

Issue #6725.  Black-box comparison against ``kicad-cli`` 10.0.x (Docker
``kicad/kicad:10.0``) gives a net's length as::

    length = sum(track runs, each trimmed at its two ends)
           + sum over vias: stackup_height(top connected layer, bottom connected layer)

This module is the single implementation of that formula.  Both measurement
forms reduce to the same neutral geometry (:class:`LSeg`, :class:`LVia`,
:class:`LPad`) and call :func:`net_length`:

* PCB form -- :func:`measure_pcb_net` (``kct check``: ``diffpair_length_skew``,
  ``match_group_length_skew``);
* Route form -- :meth:`KicadLengthModel.route_length` (the router's pair and
  match-group skew tuners).

Track runs
    Segments are first grouped into runs: unbranched chains on one layer.  Two
    segment ends continue one run only when they are the only two unclaimed
    track ends at that point (counting every layer) and share a layer.  Where
    three or more ends meet, the run grown first (input order) stops there and
    the remaining ends may still pair up.  A run never closes on itself.

Run-end trimming
    Only a run's two ends are trimmed; a run passing *through* a pad or via is
    measured in full.  A run end exactly at a pad's centre, or anywhere inside a
    via's copper, has its in-copper stretch replaced by a straight line from
    the boundary crossing to the centre (nothing changes when the run never
    leaves the copper).  Each pad or via trims at most one end of a run, and
    the run's first end wins.  Vias act first and pads second, each on the
    path the previous trims left, so a via inside a pad takes over the end
    unless it sits exactly on the pad centre.

Via height (``KicadStackup.span``)
    The copper-plus-dielectric height between the two end layers.  An outer
    copper layer (F.Cu / B.Cu) that is an end layer counts in full, an inner
    end layer counts half, everything strictly between counts in full, solder
    mask is excluded.  The stackup is the board's explicit
    ``(setup (stackup))`` when it has one, otherwise KiCad's default (0.035 mm
    copper on every layer, 0.01 mm mask per side, equal dielectrics filling
    ``(general (thickness))``).

Via span
    The span runs between the outermost layers the via is connected on: a
    layer where a track of the net ends inside the via copper, or the layer of
    a pad of the net whose copper overlaps the via (a surface pad adds its
    own layers, a through-hole pad only its footprint's side).  A via
    connected on fewer than two layers is a stub and adds no height.  The
    Route form keeps the declared span for such a via instead, because one
    router ``Route`` can be a fragment of its net -- except for a via inside a
    through-hole pad, whose barrel (not the via) carries any layer change, so
    both forms measure it as a stub (issue #6780).  Router pads carry their
    footprint's side (``Pad.side``) for the through-hole rule above.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from itertools import pairwise
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .primitives import Route

# KiCad's default stackup constants.
_DEFAULT_CU_MM = 0.035
_DEFAULT_MASK_MM = 0.01

_KEY_DIGITS = 4
_CENTRE_TOL = 5e-4


@dataclass(frozen=True)
class LSeg:
    """A track segment on a named copper layer."""

    x1: float
    y1: float
    x2: float
    y2: float
    layer: str


@dataclass(frozen=True)
class LVia:
    """A via: centre, copper diameter and the (declared) layer span."""

    x: float
    y: float
    diameter: float
    layers: tuple[str, str]


@dataclass(frozen=True)
class LPad:
    """A pad's copper outline.

    ``w``/``h`` are in the pad's own frame, rotated by ``rot`` degrees using
    KiCad's pad convention.  ``layers`` is the set of copper layer names the
    pad is on, or ``None`` for all copper layers (through-hole).  ``side`` is
    the copper layer of the footprint's side: the one layer on which a
    through-hole pad connects a via it overlaps.
    """

    x: float
    y: float
    w: float
    h: float
    rot: float = 0.0
    shape: str = "rect"
    rratio: float = 0.25
    layers: frozenset[str] | None = None
    side: str = "F.Cu"


# ---------------------------------------------------------------------------
# Stackup
# ---------------------------------------------------------------------------


def _copper_names(n: int) -> list[str]:
    if n <= 1:
        return ["F.Cu"]
    return ["F.Cu", *[f"In{i}.Cu" for i in range(1, n - 1)], "B.Cu"]


@dataclass
class KicadStackup:
    """Ordered copper/dielectric strata (top to bottom), mask excluded."""

    # (name, thickness_mm, is_copper)
    strata: list[tuple[str, float, bool]] = field(default_factory=list)

    @property
    def copper(self) -> list[str]:
        return [name for name, _t, is_cu in self.strata if is_cu]

    @classmethod
    def default(cls, thickness_mm: float, num_copper_layers: int) -> KicadStackup:
        """KiCad's default stackup for ``num_copper_layers`` in ``thickness_mm``."""
        n = max(int(num_copper_layers), 2)
        dielectric = (thickness_mm - n * _DEFAULT_CU_MM - 2 * _DEFAULT_MASK_MM) / (n - 1)
        strata: list[tuple[str, float, bool]] = []
        for i, name in enumerate(_copper_names(n)):
            strata.append((name, _DEFAULT_CU_MM, True))
            if i < n - 1:
                strata.append((f"dielectric {i + 1}", dielectric, False))
        return cls(strata)

    @classmethod
    def from_pcb(cls, pcb: PCB, num_copper_hint: int = 2) -> KicadStackup:
        """The board's explicit stackup, else KiCad's default for it.

        ``num_copper_hint`` is used only when the object carries no copper
        layer table (duck-typed boards in tests).
        """
        from kicad_tools.physics.stackup import LayerType

        pcb_copper = _pcb_copper_names(pcb)
        setup = getattr(pcb, "setup", None)
        if setup is not None and getattr(setup, "stackup", None):
            from kicad_tools.physics.stackup import Stackup

            explicit = Stackup.from_pcb(pcb)
            strata: list[tuple[str, float, bool]] = []
            for layer in explicit.layers:
                if layer.layer_type == LayerType.COPPER:
                    strata.append((layer.name, layer.thickness_mm, True))
                elif layer.layer_type == LayerType.DIELECTRIC:
                    strata.append((layer.name, layer.thickness_mm, False))
            candidate = cls(strata)
            if len(candidate.copper) >= 2:
                return candidate
        return cls.default(_pcb_thickness(pcb), len(pcb_copper) or num_copper_hint)

    def position(self, layer: str) -> int | None:
        """Index of ``layer`` among the copper layers (0 = top), or ``None``."""
        try:
            return self.copper.index(layer)
        except ValueError:
            return None

    def span(self, layer_a: str, layer_b: str) -> float:
        """KiCad stackup height between two copper layers (0 when unknown)."""
        ia = self._stratum_index(layer_a)
        ib = self._stratum_index(layer_b)
        if ia is None or ib is None or ia == ib:
            return 0.0
        lo, hi = sorted((ia, ib))
        n_copper = len(self.copper)
        cu_seen = 0
        total = 0.0
        for idx, (_name, thickness, is_cu) in enumerate(self.strata):
            cu_pos = cu_seen if is_cu else -1
            if is_cu:
                cu_seen += 1
            if idx < lo or idx > hi:
                continue
            if is_cu and idx in (lo, hi):
                outer = cu_pos in (0, n_copper - 1)
                total += thickness if outer else thickness / 2.0
            else:
                total += thickness
        return total

    def _stratum_index(self, layer: str) -> int | None:
        for idx, (name, _t, is_cu) in enumerate(self.strata):
            if is_cu and name == layer:
                return idx
        return None


def _pcb_copper_names(pcb: PCB) -> list[str]:
    from kicad_tools.core.types import CopperLayer

    names = []
    for layer in getattr(pcb, "copper_layers", ()):
        names.append(layer.name)

    def _order(name: str) -> int:
        try:
            return CopperLayer.from_kicad_name(name).stack_order
        except ValueError:
            return 10_000

    return sorted(names, key=_order)


def _pcb_thickness(pcb: PCB) -> float:
    try:
        general = pcb._sexp.find("general")
        node = general.find("thickness") if general is not None else None
        value = node.get_float(0) if node is not None else None
        if value:
            return float(value)
    except Exception:  # noqa: BLE001 - defensive: odd/hand-edited boards
        pass
    return 1.6


# ---------------------------------------------------------------------------
# Copper shapes
# ---------------------------------------------------------------------------


_DEFAULT_RRATIO = 0.25
"""KiCad's ``roundrect_rratio`` when a roundrect pad carries none."""


def _pad_rratio(pad: Any) -> float:
    """``pad.roundrect_rratio``, or KiCad's default when absent or ``None``.

    The router :class:`~kicad_tools.router.primitives.Pad` always has the
    attribute but defaults it to ``None`` (issue #6718), so a plain
    ``getattr(..., 0.25)`` would hand ``None`` to the shape test.
    """
    value = getattr(pad, "roundrect_rratio", None)
    return _DEFAULT_RRATIO if value is None else float(value)


@dataclass(frozen=True)
class _Shape:
    x: float
    y: float
    kind: str  # circle | rect | oval | roundrect
    w: float
    h: float
    rot: float = 0.0
    rratio: float | None = 0.25

    def contains(self, px: float, py: float) -> bool:
        dx, dy = px - self.x, py - self.y
        if self.kind == "circle":
            return math.hypot(dx, dy) < self.w / 2.0 - 1e-9
        if self.rot:
            from kicad_tools.core.geometry import rotate_pad_offset

            dx, dy = rotate_pad_offset(dx, dy, -self.rot)
        hw, hh = self.w / 2.0, self.h / 2.0
        ax, ay = abs(dx), abs(dy)
        eps = -1e-9  # strict: copper boundary counts as outside
        if self.kind == "oval":
            if hw >= hh:
                cx = max(ax - (hw - hh), 0.0)
                return math.hypot(cx, ay) < hh + eps
            cy = max(ay - (hh - hw), 0.0)
            return math.hypot(ax, cy) < hw + eps
        if self.kind == "roundrect":
            ratio = _DEFAULT_RRATIO if self.rratio is None else self.rratio
            r = min(ratio * min(self.w, self.h), hw, hh)
            cx = max(ax - (hw - r), 0.0)
            cy = max(ay - (hh - r), 0.0)
            if ax >= hw + eps or ay >= hh + eps:
                return False
            return r <= 0 or math.hypot(cx, cy) < r + eps
        return ax < hw + eps and ay < hh + eps

    def gap_to(self, px: float, py: float) -> float:
        """Distance from ``(px, py)`` to this copper (0 inside it).

        Every supported outline is a rectangle with rounded corners: radius 0
        (rect), a fraction of the short side (roundrect), or half the short
        side (oval, circle).
        """
        dx, dy = px - self.x, py - self.y
        if self.kind == "circle":
            return max(math.hypot(dx, dy) - self.w / 2.0, 0.0)
        if self.rot:
            from kicad_tools.core.geometry import rotate_pad_offset

            dx, dy = rotate_pad_offset(dx, dy, -self.rot)
        hw, hh = self.w / 2.0, self.h / 2.0
        if self.kind == "oval":
            r = min(hw, hh)
        elif self.kind == "roundrect":
            ratio = _DEFAULT_RRATIO if self.rratio is None else self.rratio
            r = min(ratio * min(self.w, self.h), hw, hh)
        else:
            r = 0.0
        ox = max(abs(dx) - (hw - r), 0.0)
        oy = max(abs(dy) - (hh - r), 0.0)
        return max(math.hypot(ox, oy) - r, 0.0)


def _pad_shape(pad: LPad) -> _Shape:
    kind = pad.shape if pad.shape in ("circle", "oval", "roundrect") else "rect"
    return _Shape(pad.x, pad.y, kind, pad.w, pad.h, pad.rot, pad.rratio)


def _via_shape(via: LVia) -> _Shape:
    return _Shape(via.x, via.y, "circle", via.diameter, via.diameter)


def _crossing(shape: _Shape, inside: tuple[float, float], outside: tuple[float, float]):
    """Boundary point on ``inside -> outside`` (convex shapes: one crossing)."""
    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2.0
        px = inside[0] + (outside[0] - inside[0]) * mid
        py = inside[1] + (outside[1] - inside[1]) * mid
        if shape.contains(px, py):
            lo = mid
        else:
            hi = mid
    px = inside[0] + (outside[0] - inside[0]) * lo
    py = inside[1] + (outside[1] - inside[1]) * lo
    return px, py


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, _KEY_DIGITS), round(y, _KEY_DIGITS))


Point = tuple[float, float]


# ---------------------------------------------------------------------------
# Track runs
# ---------------------------------------------------------------------------


@dataclass
class _Run:
    """An unbranched chain of segments on one layer, as an ordered polyline."""

    layer: str
    points: list[Point]

    @property
    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in pairwise(self.points))


class _RunBuilder:
    """Greedy grouping of a net's segments into :class:`_Run` polylines.

    Every segment end starts *free*.  Runs are seeded in input order and grown
    from the seed's second end, then from its first end.  At each growth point
    the free ends there are counted across all layers: growth continues only
    into a single other free end on the same layer belonging to an unused
    segment; otherwise the run's end is fixed there and stops being free.
    """

    def __init__(self, segs: list[LSeg]) -> None:
        self.segs = segs
        self.at: dict[tuple[float, float], list[tuple[int, int]]] = {}
        for idx, seg in enumerate(segs):
            for side in (0, 1):
                self.at.setdefault(_key(*self._point(idx, side)), []).append((idx, side))
        self.free = {(idx, side) for idx in range(len(segs)) for side in (0, 1)}
        self.used: set[int] = set()

    def _point(self, idx: int, side: int) -> Point:
        s = self.segs[idx]
        return (s.x1, s.y1) if side == 0 else (s.x2, s.y2)

    def _grow(self, idx: int, side: int) -> list[Point]:
        """Points appended beyond segment end ``(idx, side)``, nearest first."""
        tail: list[Point] = []
        end = (idx, side)
        while end in self.free:
            self.free.discard(end)
            here = _key(*self._point(*end))
            others = [e for e in self.at[here] if e in self.free]
            if len(others) != 1:
                break
            nxt_idx, nxt_side = others[0]
            if nxt_idx in self.used or self.segs[nxt_idx].layer != self.segs[idx].layer:
                break
            self.free.discard(others[0])
            self.used.add(nxt_idx)
            end = (nxt_idx, 1 - nxt_side)
            tail.append(self._point(*end))
        return tail

    def runs(self) -> list[_Run]:
        out: list[_Run] = []
        for idx, seg in enumerate(self.segs):
            if idx in self.used:
                continue
            self.used.add(idx)
            forward = self._grow(idx, 1)
            backward = self._grow(idx, 0)
            points = [*reversed(backward), self._point(idx, 0), self._point(idx, 1), *forward]
            out.append(_Run(seg.layer, points))
        return out


def _straighten_start(shape: _Shape, path: list[Point]) -> list[Point]:
    """``path`` with its opening stretch inside ``shape`` straightened.

    ``path[0]`` lies inside ``shape``.  The stretch up to where ``path`` first
    leaves the copper becomes a straight line from the shape's centre to that
    crossing, so the result starts at the centre.  A path that never leaves
    the copper is returned unchanged.
    """
    for i, (a, b) in enumerate(pairwise(path)):
        if not shape.contains(*b):
            return [(shape.x, shape.y), _crossing(shape, a, b), *path[i + 1 :]]
    return path


def _straighten_one_end(shape: _Shape, path: list[Point], touches) -> list[Point]:
    """Straighten the first of ``path``'s two ends for which ``touches`` holds."""
    if touches(path[0]):
        return _straighten_start(shape, path)
    if touches(path[-1]):
        return _straighten_start(shape, path[::-1])[::-1]
    return path


# ---------------------------------------------------------------------------
# The shared measurement
# ---------------------------------------------------------------------------


def _pad_on_layer(pad: LPad, layer: str) -> bool:
    return pad.layers is None or layer in pad.layers


def _via_positions(via: LVia, stackup: KicadStackup) -> tuple[int, int] | None:
    """Top and bottom stack positions of the via's declared span."""
    declared = [stackup.position(n) for n in via.layers]
    if any(p is None for p in declared):
        return None
    lo, hi = sorted(p for p in declared if p is not None)
    return lo, hi


def _via_height(
    via: LVia,
    segs: list[LSeg],
    pads: list[LPad],
    stackup: KicadStackup,
    partial_net: bool,
) -> float:
    """Stackup height charged for ``via`` (see "Via span" in the module docs)."""
    positions = _via_positions(via, stackup)
    if positions is None:
        return stackup.span(*via.layers)
    lo, hi = positions
    copper = _via_shape(via)
    radius = via.diameter / 2.0
    layers: set[int] = set()
    for s in segs:
        pos = stackup.position(s.layer)
        if pos is not None and lo <= pos <= hi:
            if copper.contains(s.x1, s.y1) or copper.contains(s.x2, s.y2):
                layers.add(pos)
    in_through_hole_pad = False
    for pad in pads:
        if _pad_shape(pad).gap_to(via.x, via.y) >= radius:
            continue
        in_through_hole_pad = in_through_hole_pad or pad.layers is None
        for name in (pad.side,) if pad.layers is None else pad.layers:
            pos = stackup.position(name)
            if pos is not None and lo <= pos <= hi:
                layers.add(pos)
    if len(layers) >= 2:
        names = stackup.copper
        return stackup.span(names[min(layers)], names[max(layers)])
    # Issue #6780: a via inside a through-hole pad is not a fragment's layer
    # change -- the pad's barrel already joins every layer -- so the Route
    # form measures it as the PCB form does (a stub) rather than assuming a
    # continuation through the via.
    return stackup.span(*via.layers) if partial_net and not in_through_hole_pad else 0.0


def net_length(
    segs: Iterable[LSeg],
    vias: Iterable[LVia],
    pads: Iterable[LPad],
    stackup: KicadStackup | None,
    *,
    partial_net: bool = False,
) -> float:
    """Length of one net exactly as kicad-cli measures it (see module docs).

    ``stackup`` of ``None`` charges vias nothing (runs and trims only).
    ``partial_net`` marks geometry that may be only part of its net (a router
    ``Route``): a via connected on fewer than two layers then keeps its
    declared span instead of counting as a zero-height stub.  Junction
    resolution follows input order, which is file order for the PCB form.
    """
    seg_list = list(segs)
    via_list = list(vias)
    pad_list = list(pads)

    total = 0.0
    for run in _RunBuilder(seg_list).runs():
        # Vias first, then pads, each acting on the path the previous one left:
        # a via straightens a run end to its own centre, so a pad still trims
        # that end only when the via sits exactly on the pad centre.
        path = run.points
        for via in via_list:
            if stackup is not None:
                positions = _via_positions(via, stackup)
                pos = stackup.position(run.layer)
                if positions is not None and (
                    pos is None or not positions[0] <= pos <= positions[1]
                ):
                    continue
            shape = _via_shape(via)
            path = _straighten_one_end(shape, path, lambda p, s=shape: s.contains(*p))
        for pad in pad_list:
            if not _pad_on_layer(pad, run.layer):
                continue
            centre = (pad.x, pad.y)
            path = _straighten_one_end(
                _pad_shape(pad), path, lambda p, c=centre: math.dist(p, c) <= _CENTRE_TOL
            )
        total += _Run(run.layer, path).length

    if stackup is not None:
        for via in via_list:
            total += _via_height(via, seg_list, pad_list, stackup, partial_net)
    return total


# ---------------------------------------------------------------------------
# PCB form
# ---------------------------------------------------------------------------


def _pcb_pads(pcb: PCB, net_id: int) -> list[LPad]:
    from kicad_tools.core.geometry import rotate_pad_offset

    pads: list[LPad] = []
    for fp in getattr(pcb, "footprints", ()):
        for pad in fp.pads:
            if pad.net_number != net_id:
                continue
            rx, ry = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
            layers = None
            if pad.type != "thru_hole" and "*.Cu" not in pad.layers:
                layers = frozenset(n for n in pad.layers if n.endswith(".Cu"))
            pads.append(
                LPad(
                    x=fp.position[0] + rx,
                    y=fp.position[1] + ry,
                    w=pad.size[0],
                    h=pad.size[1],
                    rot=pad.rotation,
                    shape=pad.shape,
                    rratio=_pad_rratio(pad),
                    layers=layers,
                    side="B.Cu" if getattr(fp, "layer", "F.Cu") == "B.Cu" else "F.Cu",
                )
            )
    return pads


def pcb_net_geometry(pcb: PCB, net_id: int) -> tuple[list[LSeg], list[LVia], list[LPad]]:
    """Neutral geometry of ``net_id`` on a routed PCB."""
    segs = [
        LSeg(s.start[0], s.start[1], s.end[0], s.end[1], s.layer)
        for s in pcb.segments_in_net(net_id)
    ]
    vias = []
    for v in pcb.vias_in_net(net_id):
        if not v.layers or len(v.layers) < 2:
            continue
        vias.append(LVia(v.position[0], v.position[1], v.size, (v.layers[0], v.layers[-1])))
    return segs, vias, _pcb_pads(pcb, net_id)


def measure_pcb_net(
    pcb: PCB,
    net_id: int,
    stackup: KicadStackup | None = None,
    num_copper_hint: int = 2,
) -> float:
    """kicad-cli net length of ``net_id`` read from a routed PCB."""
    segs, vias, pads = pcb_net_geometry(pcb, net_id)
    return net_length(segs, vias, pads, stackup or KicadStackup.from_pcb(pcb, num_copper_hint))


# ---------------------------------------------------------------------------
# Route form
# ---------------------------------------------------------------------------


def _lname(layer: Any) -> str:
    return getattr(layer, "kicad_name", None) or str(layer)


def route_geometry(route: Route) -> tuple[list[LSeg], list[LVia]]:
    """Neutral segments and vias of a router :class:`Route`.

    Issue #6792: a via listed more than once (same position, diameter and
    span) is one physical via and is returned once.  The tuners measure a net
    held as several fragments by merging them into one Route, and fragments
    that meet at a via each carry it; charging its span per copy over-counts
    the net (by ~1.05 mm for In1.Cu + In2.Cu fragments sharing a via in a
    through-hole pad on a 1.6 mm 4-layer board).
    """
    segs = [LSeg(s.x1, s.y1, s.x2, s.y2, _lname(s.layer)) for s in route.segments]
    vias: list[LVia] = []
    seen: set[tuple[float, float, float, str, str]] = set()
    for v in route.vias:
        a, b = getattr(v, "drilled_layers", v.layers)
        la, lb = _lname(a), _lname(b)
        key = (round(v.x, 6), round(v.y, 6), round(v.diameter, 6), la, lb)
        if key in seen:
            continue
        seen.add(key)
        vias.append(LVia(v.x, v.y, v.diameter, (la, lb)))
    return segs, vias


def router_pad(pad: Any) -> LPad:
    """Neutral pad from a router :class:`~kicad_tools.router.primitives.Pad`."""
    through = bool(getattr(pad, "through_hole", False))
    layers = None if through else frozenset({_lname(pad.layer)})
    return LPad(
        x=pad.x,
        y=pad.y,
        w=pad.width,
        h=pad.height,
        rot=getattr(pad, "rotation", 0.0) or 0.0,
        shape=getattr(pad, "shape", None) or "rect",
        rratio=_pad_rratio(pad),
        layers=layers,
        # Issue #6780: the loaders record the footprint side; a programmatic
        # pad without one falls back to its own layer.
        side=_lname(getattr(pad, "side", None) or pad.layer),
    )


@dataclass
class KicadLengthModel:
    """Everything needed to measure a router :class:`Route` the kicad-cli way.

    ``pads_by_net`` carries the pad copper each route end can enter.  Build one
    with :meth:`from_pcb` (board file), :meth:`for_router` (live router) or
    :meth:`default` (thickness + layer count only, no pad trimming).
    """

    stackup: KicadStackup
    pads_by_net: dict[int, list[LPad]] = field(default_factory=dict)

    @classmethod
    def default(cls, thickness_mm: float, num_copper_layers: int) -> KicadLengthModel:
        return cls(KicadStackup.default(thickness_mm, num_copper_layers))

    @classmethod
    def from_pcb(cls, pcb: PCB) -> KicadLengthModel:
        pads_by_net: dict[int, list[LPad]] = {}
        net_ids = {p.net_number for fp in getattr(pcb, "footprints", ()) for p in fp.pads}
        for net_id in net_ids:
            if net_id:
                pads_by_net[net_id] = _pcb_pads(pcb, net_id)
        return cls(KicadStackup.from_pcb(pcb), pads_by_net)

    @classmethod
    def for_router(cls, stackup: KicadStackup, pads: Iterable[Any]) -> KicadLengthModel:
        pads_by_net: dict[int, list[LPad]] = {}
        for pad in pads:
            if getattr(pad, "net", 0):
                pads_by_net.setdefault(pad.net, []).append(router_pad(pad))
        return cls(stackup, pads_by_net)

    def route_length(self, route: Route) -> float:
        segs, vias = route_geometry(route)
        return net_length(
            segs, vias, self.pads_by_net.get(route.net, []), self.stackup, partial_net=True
        )

    def net_length_pcb(self, pcb: PCB, net_id: int) -> float:
        segs, vias, _pads = pcb_net_geometry(pcb, net_id)
        return net_length(segs, vias, self.pads_by_net.get(net_id, []), self.stackup)
