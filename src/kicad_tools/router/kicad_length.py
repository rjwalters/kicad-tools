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
    ends inside the via, or a pad of the net sits on it), not its declared
    ``(layers ...)``.  A via connected on fewer than two layers falls back to
    the declared span.

Pad/via trimming
    A track chain entering a pad or via copper is clipped at the copper
    boundary and the in-copper portion is replaced by a straight line to the
    centre.  Zero when the track runs straight to the centre.
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


@dataclass(frozen=True)
class _Shape:
    x: float
    y: float
    kind: str  # circle | rect | oval | roundrect
    w: float
    h: float
    rot: float = 0.0
    rratio: float = 0.25

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
            r = min(self.rratio * min(self.w, self.h), hw, hh)
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


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x, _KEY_DIGITS), round(y, _KEY_DIGITS))


def _trim_for_shape(shape: _Shape, segs: list[LSeg]) -> float:
    """Length removed from ``segs`` (one layer) by clipping at ``shape``."""
    if not any(shape.contains(s.x1, s.y1) or shape.contains(s.x2, s.y2) for s in segs):
        return 0.0
    adj: dict[tuple[float, float], list[int]] = {}
    for i, s in enumerate(segs):
        adj.setdefault(_key(s.x1, s.y1), []).append(i)
        adj.setdefault(_key(s.x2, s.y2), []).append(i)

    def pt(i: int, end: int) -> tuple[float, float]:
        s = segs[i]
        return (s.x1, s.y1) if end == 0 else (s.x2, s.y2)

    visited: set[int] = set()
    trim = 0.0
    centre = (shape.x, shape.y)
    for i in range(len(segs)):
        for end in (0, 1):
            if i in visited or not shape.contains(*pt(i, end)):
                continue
            # A walk starts only where a track ends at the pad/via centre
            # (kicad-cli leaves a track that ends elsewhere inside the copper
            # untouched).  A vertex inside the copper that merely continues the
            # chain is reached by the walk from the chain's own end.
            if math.dist(pt(i, end), centre) > _CENTRE_TOL:
                continue
            cur, entry = i, end
            inside = 0.0
            while True:
                visited.add(cur)
                a, b = pt(cur, entry), pt(cur, 1 - entry)
                if not shape.contains(*b):
                    x = _crossing(shape, a, b)
                    inside += math.dist(a, x)
                    trim += inside - math.dist(x, centre)
                    break
                inside += math.dist(a, b)
                nxt = [j for j in adj.get(_key(*b), []) if j not in visited]
                if len(nxt) != 1:
                    # The chain never leaves the copper (ends or forks inside
                    # it): kicad-cli measures it untouched.
                    break
                j = nxt[0]
                cur = j
                entry = 0 if _key(*pt(j, 0)) == _key(*b) else 1
    return trim


# ---------------------------------------------------------------------------
# The shared measurement
# ---------------------------------------------------------------------------


def _pad_on_layer(pad: LPad, layer: str) -> bool:
    return pad.layers is None or layer in pad.layers


def _via_connected_span(
    via: LVia,
    segs: list[LSeg],
    pads: list[LPad],
    stackup: KicadStackup,
) -> tuple[str, str]:
    """Layers the via connects on (min/max by stack position), else declared."""
    declared = [stackup.position(n) for n in via.layers]
    if any(p is None for p in declared):
        return via.layers
    lo_d, hi_d = sorted(p for p in declared if p is not None)
    shape = _via_shape(via)
    copper = stackup.copper
    found: set[int] = set()
    for s in segs:
        pos = stackup.position(s.layer)
        if pos is None or not (lo_d <= pos <= hi_d):
            continue
        if shape.contains(s.x1, s.y1) or shape.contains(s.x2, s.y2):
            found.add(pos)
    for pad in pads:
        if not _pad_shape(pad).contains(via.x, via.y):
            continue
        for pos in range(lo_d, hi_d + 1):
            if _pad_on_layer(pad, copper[pos]):
                found.add(pos)
    if len(found) >= 2:
        return copper[min(found)], copper[max(found)]
    return via.layers


def net_length(
    segs: Iterable[LSeg],
    vias: Iterable[LVia],
    pads: Iterable[LPad],
    stackup: KicadStackup | None,
) -> float:
    """Length of one net exactly as kicad-cli measures it (see module docs).

    ``stackup`` of ``None`` charges vias nothing (segments and trims only).
    """
    seg_list = list(segs)
    via_list = list(vias)
    pad_list = list(pads)

    total = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in seg_list)

    by_layer: dict[str, list[LSeg]] = {}
    for s in seg_list:
        by_layer.setdefault(s.layer, []).append(s)

    for pad in pad_list:
        shape = _pad_shape(pad)
        for layer, layer_segs in by_layer.items():
            if _pad_on_layer(pad, layer):
                total -= _trim_for_shape(shape, layer_segs)

    for via in via_list:
        shape = _via_shape(via)
        positions = None
        if stackup is not None:
            declared = [stackup.position(n) for n in via.layers]
            if all(p is not None for p in declared):
                positions = sorted(p for p in declared if p is not None)
        for layer, layer_segs in by_layer.items():
            if stackup is not None and positions is not None:
                pos = stackup.position(layer)
                if pos is None or not (positions[0] <= pos <= positions[-1]):
                    continue
            total -= _trim_for_shape(shape, layer_segs)

    if stackup is not None:
        for via in via_list:
            top, bottom = _via_connected_span(via, seg_list, pad_list, stackup)
            total += stackup.span(top, bottom)
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
                    rratio=getattr(pad, "roundrect_rratio", 0.25),
                    layers=layers,
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
        shape=getattr(pad, "shape", "rect"),
        rratio=getattr(pad, "roundrect_rratio", 0.25),
        layers=layers,
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
        return net_length(segs, vias, self.pads_by_net.get(route.net, []), self.stackup)

    def net_length_pcb(self, pcb: PCB, net_id: int) -> float:
        segs, vias, _pads = pcb_net_geometry(pcb, net_id)
        return net_length(segs, vias, self.pads_by_net.get(net_id, []), self.stackup)
