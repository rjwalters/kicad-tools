"""KiCad-exact net length: the one measurement shared by checker and tuners.

Issue #6725.  ``kicad-cli`` (10.0.x) computes a net's length as::

    length = sum(segment lengths)
           - sum over each pad/via a track chain enters:
                 (chain length inside the pad/via copper)
               - (straight line from the boundary crossing to the centre)
           + sum over vias: stackup_height(top connected layer, bottom connected layer)

This module is the single implementation of that formula.  Both measurement
forms reduce to the same neutral geometry (:class:`LSeg`, :class:`LVia`,
:class:`LPad`) and call :func:`net_length`:

* PCB form -- :func:`measure_pcb_net` (``kct check``: ``diffpair_length_skew``,
  ``match_group_length_skew``);
* Route form -- :meth:`KicadLengthModel.route_length` (the router's pair and
  match-group skew tuners).

Via height (``KicadStackup.span``)
    The copper-plus-dielectric height between the two end layers.  An outer
    copper layer (F.Cu / B.Cu) that is an end layer counts in full, an inner
    end layer counts half, everything strictly between counts in full, solder
    mask is excluded.  The stackup is the board's explicit
    ``(setup (stackup))`` when it has one, otherwise KiCad's default (0.035 mm
    copper on every layer, 0.01 mm mask per side, equal dielectrics filling
    ``(general (thickness))``).

Via span
    The span uses the layers the via actually connects on (a track of the net
    ends at the via centre or inside its copper, or the via sits in a pad of
    the net: the pad's side layer), not its declared ``(layers ...)``.  A via
    connected on fewer than two layers (a stub, or one fed only by a zone)
    gets zero height, as in kicad-cli's ``optimiseVias`` -- except in the
    Route form, where a Route may be only a fragment of the net and such a
    via keeps its declared span (``net_length(..., fragment=True)``).

Lines (segment merging)
    Segments are first merged into lines the way kicad-cli does: two lines
    join only where exactly two line endpoints meet, on the same layer.  A
    pad or via centre that a track passes *through* is therefore not a line
    end, and a junction of three or more tracks splits the net.

Pad/via trimming
    A line with an end inside a via's copper (anywhere inside, not only at
    the centre) is clipped at the via boundary and the in-copper portion is
    replaced by a straight line to the via centre.  A line is clipped at a pad
    only when one of its ends is exactly at the pad's centre.  A line that
    never leaves the copper is left untouched.  Zero when the track runs
    straight to the centre.

Issue #6725 follow-up: the bench comparison against kicad-cli 10.0.6 on boards
03/06b/06rf/07rf found the merge step and the via "inside, not centre" rule;
both mirror ``pcbnew/length_delay_calculation`` (behaviour only, no code).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

    from .primitives import Route

# KiCad's default stackup constants.
_DEFAULT_CU_MM = 0.035
_DEFAULT_MASK_MM = 0.01

_NM_PER_MM = 1_000_000
"""KiCad's internal unit: positions compare exactly in integer nanometres."""


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
    pad is on, or ``None`` for all copper layers (through-hole).
    """

    x: float
    y: float
    w: float
    h: float
    rot: float = 0.0
    shape: str = "rect"
    rratio: float = 0.25
    layers: frozenset[str] | None = None
    #: The footprint's side (``F.Cu``/``B.Cu``): the layer a via sitting in a
    #: through-hole pad picks up (kicad-cli ``optimiseVias``).
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


# ---------------------------------------------------------------------------
# The shared measurement
# ---------------------------------------------------------------------------


def _key(x: float, y: float) -> tuple[int, int]:
    """A point as KiCad holds it: integer nanometres (exact-match comparisons)."""
    return (round(x * _NM_PER_MM), round(y * _NM_PER_MM))


@dataclass
class _Line:
    """A KiCad length-calculation line: a polyline on one copper layer."""

    pts: list[tuple[float, float]]
    layer: str
    status: str = "unmerged"  # unmerged | in_use | retired

    def length(self) -> float:
        return sum(math.dist(a, b) for a, b in zip(self.pts, self.pts[1:], strict=False))


def _merge_lines(lines: list[_Line]) -> None:
    """Join lines end to end exactly as kicad-cli 10's ``mergeLines`` does.

    Every line endpoint is entered in one position map (all layers together).
    Lines are taken in order; each unmerged line becomes a primary, is removed
    from the map, and repeatedly absorbs the single remaining line at its start
    and then its end -- only when exactly one other line endpoint is left at
    that point, and only if it is on the same layer.  A junction of three or
    more lines (or two lines on different layers, e.g. at a via) therefore
    splits the net into separate lines, and the outcome at a 3+-way junction
    depends on the line order.
    """
    pos: dict[tuple[int, int], dict[int, None]] = {}
    for idx, line in enumerate(lines):
        pos.setdefault(_key(*line.pts[0]), {})[idx] = None
        pos.setdefault(_key(*line.pts[-1]), {})[idx] = None

    def remove(idx: int) -> None:
        line = lines[idx]
        pos.get(_key(*line.pts[0]), {}).pop(idx, None)
        pos.get(_key(*line.pts[-1]), {}).pop(idx, None)

    def try_merge(primary: _Line, at_start: bool) -> bool:
        point = primary.pts[0] if at_start else primary.pts[-1]
        items = pos.get(_key(*point))
        if not items or len(items) != 1:
            return False
        other_idx = next(iter(items))
        other = lines[other_idx]
        if other.layer != primary.layer:
            return False
        other.status = "retired"
        if at_start:
            sec = other.pts if _key(*other.pts[-1]) == _key(*point) else other.pts[::-1]
            primary.pts = sec[:-1] + primary.pts
        else:
            sec = other.pts if _key(*other.pts[0]) == _key(*point) else other.pts[::-1]
            primary.pts = primary.pts + sec[1:]
        remove(other_idx)
        return True

    for idx, primary in enumerate(lines):
        if primary.status != "unmerged":
            continue
        remove(idx)
        primary.status = "in_use"
        while True:
            merged_start = try_merge(primary, at_start=True)
            merged_end = try_merge(primary, at_start=False)
            if not merged_start and not merged_end:
                break


def _clip(line: _Line, shape: _Shape, forward: bool) -> None:
    """kicad-cli's ``clipLineToPad`` / ``clipLineToVia``.

    From the end at ``forward`` (start) or the other end, find the first vertex
    outside ``shape``; replace everything before it with
    ``centre -> boundary crossing``.  A line that never leaves the copper is
    left untouched.
    """
    pts = line.pts if forward else line.pts[::-1]
    first_out = next((i for i in range(1, len(pts)) if not shape.contains(*pts[i])), None)
    if first_out is None:
        return
    crossing = _crossing(shape, pts[first_out - 1], pts[first_out])
    new = [(shape.x, shape.y), crossing, *pts[first_out:]]
    line.pts = new if forward else new[::-1]


def _pad_on_layer(pad: LPad, layer: str) -> bool:
    return pad.layers is None or layer in pad.layers


def _pad_side_layer(pad: LPad, stackup: KicadStackup) -> str:
    """The layer kicad-cli adds to a via's span for a pad the via sits in.

    SMD: the pad's first copper layer in stack order; through-hole: the
    footprint's side.
    """
    if pad.layers is None:
        return pad.side
    on = [n for n in stackup.copper if n in pad.layers]
    return on[0] if on else pad.side


def _via_connected_layers(
    via: LVia,
    lines: list[_Line],
    pads: list[LPad],
    stackup: KicadStackup,
) -> tuple[str, str] | None:
    """kicad-cli's ``optimiseVias`` span: ``None`` when it measures no height.

    Layers of every (unmerged) line with an endpoint at the via centre or
    inside the via copper, plus the side layer of a pad the via sits in.  Two
    or more layers: the outermost two by stack position.  One layer (a stub
    via) or none: kicad-cli charges the via zero height.
    """
    shape = _via_shape(via)
    centre = _key(via.x, via.y)
    layers: set[str] = set()
    for line in lines:
        for end in (line.pts[0], line.pts[-1]):
            if _key(*end) == centre or shape.contains(*end):
                layers.add(line.layer)
    for pad in pads:
        if _pad_shape(pad).contains(via.x, via.y):
            layers.add(_pad_side_layer(pad, stackup))
            break
    positions = sorted(p for n in layers if (p := stackup.position(n)) is not None)
    if len(positions) < 2:
        return None
    return stackup.copper[positions[0]], stackup.copper[positions[-1]]


def net_length(
    segs: Iterable[LSeg],
    vias: Iterable[LVia],
    pads: Iterable[LPad],
    stackup: KicadStackup | None,
    fragment: bool = False,
) -> float:
    """Length of one net exactly as kicad-cli measures it (see module docs).

    Mirrors kicad-cli 10's DRC length calculation step by step:

    1. via spans from the raw segments (:func:`_via_connected_layers`);
    2. segments merged into lines (:func:`_merge_lines`);
    3. every line with an end inside a via's copper (anywhere inside, not only
       at the centre) is clipped at that via (:func:`_clip`), one end per via;
    4. every line with an end exactly at a pad's centre, on a layer the pad is
       on, is clipped at that pad -- a line that merely passes through a pad
       centre (merged across it) is not;
    5. length = sum of line lengths + sum of via stackup heights.

    ``stackup`` of ``None`` charges vias nothing (segments and clipping only).

    ``fragment``: the geometry may be only part of the net (a router
    :class:`Route` -- e.g. a pad-to-via escape whose continuation on the other
    layer is a separate Route).  A via whose tracks here use fewer than two
    layers then measures its declared span instead of kicad-cli's zero, which
    is what the whole net measures when the continuation exists.  The PCB
    form always sees the whole net and uses kicad-cli's rule.
    """
    lines = [_Line([(s.x1, s.y1), (s.x2, s.y2)], s.layer) for s in segs]
    via_list = list(vias)
    pad_list = list(pads)

    via_height = 0.0
    if stackup is not None:
        for via in via_list:
            span = _via_connected_layers(via, lines, pad_list, stackup)
            if span is None and fragment:
                span = via.layers
            if span is not None:
                via_height += stackup.span(*span)

    _merge_lines(lines)
    live = [line for line in lines if line.status == "in_use"]

    for via in via_list:
        shape = _via_shape(via)
        for line in live:
            if shape.contains(*line.pts[0]):
                _clip(line, shape, forward=True)
            elif shape.contains(*line.pts[-1]):
                _clip(line, shape, forward=False)

    for pad in pad_list:
        shape = _pad_shape(pad)
        centre = _key(pad.x, pad.y)
        for line in live:
            if _key(*line.pts[0]) != centre and _key(*line.pts[-1]) != centre:
                continue
            if not _pad_on_layer(pad, line.layer):
                continue
            if shape.contains(*line.pts[0]):
                _clip(line, shape, forward=True)
            elif shape.contains(*line.pts[-1]):
                _clip(line, shape, forward=False)

    return sum(line.length() for line in live) + via_height


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
    """Neutral segments and vias of a router :class:`Route`."""
    segs = [LSeg(s.x1, s.y1, s.x2, s.y2, _lname(s.layer)) for s in route.segments]
    vias = []
    for v in route.vias:
        a, b = getattr(v, "drilled_layers", v.layers)
        vias.append(LVia(v.x, v.y, v.diameter, (_lname(a), _lname(b))))
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
        side="B.Cu" if _lname(getattr(pad, "layer", "F.Cu")) == "B.Cu" else "F.Cu",
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
            segs, vias, self.pads_by_net.get(route.net, []), self.stackup, fragment=True
        )

    def net_length_pcb(self, pcb: PCB, net_id: int) -> float:
        segs, vias, _pads = pcb_net_geometry(pcb, net_id)
        return net_length(segs, vias, self.pads_by_net.get(net_id, []), self.stackup)
