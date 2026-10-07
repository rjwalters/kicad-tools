"""Copy schematic pin electrical types onto PCB pads (issue #5985).

KiCad's "Update PCB from Schematic" writes two children onto every pad of a
footprint that has a schematic symbol::

    (pad "1" smd rect ... (net 3 "+3V3") (pinfunction "VDD") (pintype "power_in") ...)

``pinfunction`` is the symbol pin *name* and ``pintype`` its electrical type
(``power_in``, ``power_out``, ``passive``, ``input``, ...).  For a pin that
carries a schematic no-connect flag (the ``X`` marker) KiCad appends
``+no_connect`` to the type (``passive+no_connect``).

The suffix is a property of the *schematic*, not of the pad's PCB net: KiCad
gives every unconnected pin a single-pad ``unconnected-(REF-PIN-PadN)`` net
whether or not it is flagged, and writes the suffix only for flagged pins
(KiCad 10's Edgeberry and STM32 Nucleo-64 templates have unflagged
``unconnected-(...)`` pads typed plain ``passive``/``output``).  Deriving it
from the schematic alone also makes both entry points below agree regardless
of how -- or whether -- nets have been assigned yet.

The kct board generators build ``.kicad_pcb`` files directly rather than
through pcbnew, so until this module they never carried that data and the
pin-type rail evidence in :func:`kicad_tools.explain.mistakes.power_pin_nets`
(issue #5939) never ran on the project's own boards.

Two entry points share one schematic-derived map:

* :func:`annotate_pcb_pintypes` edits a loaded :class:`~kicad_tools.schema.pcb.PCB`
  (in-memory pads and the S-expression tree).  Used by ``kct pcb
  sync-netlist`` after it assigns nets.
* :func:`annotate_pcb_file_pintypes` rewrites a ``.kicad_pcb`` file **in
  place, preserving its existing text byte-for-byte** apart from the inserted
  or replaced ``pinfunction``/``pintype`` children.  Board generators call it
  right after writing the unrouted PCB; the router merges tracks into that
  text, so the routed PCB inherits the annotation.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)

__all__ = [
    "PadPinInfo",
    "PinTypeAnnotation",
    "annotate_pcb_file_pintypes",
    "annotate_pcb_pintypes",
    "pad_pin_info_map",
    "schematic_pin_info",
]

#: Suffix KiCad appends to the pin type of an unconnected pin.
NO_CONNECT_SUFFIX = "+no_connect"


@dataclass(frozen=True)
class PadPinInfo:
    """Schematic pin data destined for one PCB pad."""

    pinfunction: str
    pintype: str
    #: The schematic pin carries a no-connect flag (``+no_connect`` suffix).
    no_connect: bool = False

    @property
    def effective_pintype(self) -> str:
        """The ``pintype`` value KiCad writes on the pad."""
        # A pin already typed ``no_connect`` is not suffixed a second time.
        if (
            self.no_connect
            and self.pintype != "no_connect"
            and not self.pintype.endswith(NO_CONNECT_SUFFIX)
        ):
            return self.pintype + NO_CONNECT_SUFFIX
        return self.pintype


@dataclass
class PinTypeAnnotation:
    """Result of an annotation pass."""

    #: Pads whose ``pinfunction``/``pintype`` were written or changed.
    updated: int = 0
    #: Pads that already carried the expected values.
    unchanged: int = 0
    #: ``REF.PAD`` keys the schematic defines but the PCB has no pad for.
    missing_pads: list[str] = field(default_factory=list)

    @property
    def annotated(self) -> int:
        """Pads that carry schematic pin data after the pass."""
        return self.updated + self.unchanged


#: Coordinate tolerance (mm) when matching a pin end against a no-connect
#: flag or a wire.  Raw file coordinates are compared, so float noise on a
#: 0.635 mm half-grid (``…x.xx5``) cannot flip the result the way exact
#: equality after ``round(…, 2)`` could.
_POINT_TOLERANCE = 1e-3

_Point = tuple[float, float]
_Segment = tuple[float, float, float, float]


def _on_segment(px: float, py: float, seg: _Segment, tol: float = _POINT_TOLERANCE) -> bool:
    """Whether ``(px, py)`` lies on the segment (endpoints included)."""
    x1, y1, x2, y2 = seg
    dx, dy = x2 - x1, y2 - y1
    length_sq = dx * dx + dy * dy
    if length_sq <= tol * tol:
        return abs(px - x1) <= tol and abs(py - y1) <= tol
    t = ((px - x1) * dx + (py - y1) * dy) / length_sq
    t = min(1.0, max(0.0, t))
    cx, cy = x1 + t * dx, y1 + t * dy
    return (px - cx) ** 2 + (py - cy) ** 2 <= tol * tol


def _at_wire_end(px: float, py: float, seg: _Segment, tol: float = _POINT_TOLERANCE) -> bool:
    """Whether ``(px, py)`` is one of the wire's two end points."""
    x1, y1, x2, y2 = seg
    return (abs(px - x1) <= tol and abs(py - y1) <= tol) or (
        abs(px - x2) <= tol and abs(py - y2) <= tol
    )


def _segments_touch(a: _Segment, b: _Segment) -> bool:
    """KiCad wire-to-wire connection without a junction: a shared end point.

    An end point landing on another wire's *interior* (a bare "T") does not
    connect in KiCad's netlist; that needs a junction, handled separately.
    """
    return _at_wire_end(a[0], a[1], b) or _at_wire_end(a[2], a[3], b)


#: Cell size (mm) of the point hash grid.  Four times the tolerance, so two
#: points within the tolerance box always land in the same or an adjacent
#: cell, with a wide margin for float rounding in ``x / cell``.
_POINT_CELL = 4 * _POINT_TOLERANCE

#: Cell size (mm) of the wire hash grid used to find the wires a junction
#: lies on.  One schematic grid step: a typical wire spans a handful of cells.
_WIRE_CELL = 2.54

#: A wire whose bounding box covers more grid cells than this (absurd
#: coordinates) is checked against every junction instead of being bucketed.
_MAX_WIRE_CELLS = 4096


def _finite(*values: float) -> bool:
    return all(math.isfinite(v) for v in values)


class _PointIndex:
    """Hash grid of points, answering "which stored points are within the
    tolerance box of ``(x, y)``" with the exact same test as
    :func:`_at_wire_end` (``abs(dx) <= tol and abs(dy) <= tol``).

    The 3x3 neighbourhood of cells covers every point the box test can
    accept, and each candidate is re-checked exactly, so the index changes
    the cost of a lookup but never its answer.  A non-finite coordinate
    never passes the box test, so such points are simply not stored.
    """

    def __init__(self) -> None:
        self._grid: dict[tuple[int, int], list[tuple[float, float, int]]] = {}

    def add(self, x: float, y: float, item: int) -> None:
        if _finite(x, y):
            key = (math.floor(x / _POINT_CELL), math.floor(y / _POINT_CELL))
            self._grid.setdefault(key, []).append((x, y, item))

    def near(self, x: float, y: float, tol: float = _POINT_TOLERANCE) -> Iterator[int]:
        if not _finite(x, y):
            return
        cx, cy = math.floor(x / _POINT_CELL), math.floor(y / _POINT_CELL)
        grid = self._grid
        for i in (cx - 1, cx, cx + 1):
            for j in (cy - 1, cy, cy + 1):
                for px, py, item in grid.get((i, j), ()):
                    if abs(px - x) <= tol and abs(py - y) <= tol:
                        yield item


def _wires_through_junctions(wires: list[_Segment], junctions: list[_Point]) -> Iterator[list[int]]:
    """For each junction, the indices of the wires it lies on (:func:`_on_segment`).

    Wires are bucketed into a coarse grid by their bounding box, grown by a
    margin larger than the tolerance; a point within the tolerance of a
    segment lies inside that box, so only the junction's own cell needs
    checking.  Candidates are re-checked with :func:`_on_segment` exactly.
    """
    margin = 2 * _POINT_TOLERANCE
    grid: dict[tuple[int, int], list[int]] = {}
    unbucketed: list[int] = []
    for idx, (x1, y1, x2, y2) in enumerate(wires):
        if not _finite(x1, y1, x2, y2):
            unbucketed.append(idx)
            continue
        i0 = math.floor((min(x1, x2) - margin) / _WIRE_CELL)
        i1 = math.floor((max(x1, x2) + margin) / _WIRE_CELL)
        j0 = math.floor((min(y1, y2) - margin) / _WIRE_CELL)
        j1 = math.floor((max(y1, y2) + margin) / _WIRE_CELL)
        if (i1 - i0 + 1) * (j1 - j0 + 1) > _MAX_WIRE_CELLS:
            unbucketed.append(idx)
            continue
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                grid.setdefault((i, j), []).append(idx)
    for jx, jy in junctions:
        # _on_segment never accepts a non-finite junction.
        if not _finite(jx, jy):
            continue
        key = (math.floor(jx / _WIRE_CELL), math.floor(jy / _WIRE_CELL))
        candidates = grid.get(key, [])
        on = [i for i in candidates if _on_segment(jx, jy, wires[i])]
        on.extend(i for i in unbucketed if _on_segment(jx, jy, wires[i]))
        if len(on) > 1:
            yield on


@dataclass
class _NoConnectZone:
    """Everything on one sheet that a no-connect flag electrically reaches.

    KiCad writes ``+no_connect`` for every pin in a connection subgraph that
    holds a no-connect flag.  A subgraph is per sheet and built from wires,
    so a pin is flagged when the flag sits on its end point *or* on a wire
    network the pin touches (the "stub wire ending in an X" style).  Labels
    join subgraphs into nets but do not share the flag, so they are not
    followed.

    Connection rules, each checked against ``kicad-cli sch export netlist``:
    pins and flags join a wire only at its end points; wires join at shared
    end points, or where a junction sits on both.  A pin or flag on a wire's
    interior, or a bare "T" without a junction, does not connect.

    :meth:`build` runs in near-linear time (issue #6016): wire end points and
    wires are bucketed in hash grids, connectivity is a union-find over the
    wires, and :meth:`covers` is a grid lookup.  Every grid hit is re-checked
    with the same tolerance test as before, so results are unchanged.
    """

    points: list[_Point]
    wires: list[_Segment]
    _index: _PointIndex = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        # Everything a pin end may touch to be flagged: the flags themselves
        # and the end points of every reached wire.
        index = _PointIndex()
        for x, y in self.points:
            index.add(x, y, 0)
        for x1, y1, x2, y2 in self.wires:
            index.add(x1, y1, 0)
            index.add(x2, y2, 0)
        self._index = index

    @classmethod
    def build(
        cls, points: list[_Point], wires: list[_Segment], junctions: list[_Point]
    ) -> _NoConnectZone:
        parent = list(range(len(wires)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        ends = _PointIndex()
        for idx, (x1, y1, x2, y2) in enumerate(wires):
            ends.add(x1, y1, idx)
            ends.add(x2, y2, idx)

        # Wires sharing an end point (_segments_touch).
        for idx, (x1, y1, x2, y2) in enumerate(wires):
            for other in ends.near(x1, y1):
                union(idx, other)
            for other in ends.near(x2, y2):
                union(idx, other)

        # Wires joined by a junction sitting on both.
        for on in _wires_through_junctions(wires, junctions):
            for other in on[1:]:
                union(on[0], other)

        # Like a pin, a flag joins a wire only at one of the wire's end points.
        seeds = {find(idx) for x, y in points for idx in ends.near(x, y)}
        reached = [w for idx, w in enumerate(wires) if find(idx) in seeds]
        return cls(points, reached)

    def covers(self, x: float, y: float) -> bool:
        # A pin joins a wire only at the wire's end points; one lying on a
        # wire's interior is not connected (KiCad's own netlist agrees).
        return next(self._index.near(x, y), None) is not None


@dataclass(frozen=True)
class _Placement:
    """A placed symbol's raw ``(at x y rot)`` and ``(mirror …)`` axis."""

    x: float
    y: float
    rotation: float
    mirror: str


def _xy(node) -> _Point | None:
    if node is None:
        return None
    x, y = node.get_float(0), node.get_float(1)
    if x is None or y is None:
        return None
    return (x, y)


def _read_sheet_geometry(
    path: Path,
) -> tuple[dict[str, tuple[float, float, float]], _NoConnectZone | None]:
    """Raw ``(x, y, rotation)`` (by symbol UUID) and the no-connect zone of one sheet.

    Coordinates are read from the raw file rather than the
    :class:`~kicad_tools.schematic.models.Schematic` model, which rounds them
    to 0.01 mm.  The mirror axis comes from the model (issue #6005).
    """
    from kicad_tools.sexp import parse_file

    root = parse_file(path)
    points = [p for p in (_xy(n.find_child("at")) for n in root.find_children("no_connect")) if p]
    if not points:
        return {}, None
    placements: dict[str, tuple[float, float, float]] = {}
    for node in root.find_children("symbol"):
        uuid_node = node.find_child("uuid")
        at = node.find_child("at")
        xy = _xy(at)
        if uuid_node is None or at is None or xy is None:
            continue
        placements[uuid_node.get_string(0) or ""] = (xy[0], xy[1], at.get_float(2) or 0.0)
    wires: list[_Segment] = []
    for node in root.find_children("wire"):
        pts = node.find_child("pts")
        ends = [_xy(p) for p in pts.find_children("xy")] if pts is not None else []
        if len(ends) == 2 and ends[0] and ends[1]:
            wires.append((*ends[0], *ends[1]))
    junctions = [p for p in (_xy(n.find_child("at")) for n in root.find_children("junction")) if p]
    return placements, _NoConnectZone.build(points, wires, junctions)


def _iter_schematic_symbols(sch_path: Path):
    """Yield ``(reference, symbol, placement, no_connect_zone)`` across the hierarchy.

    ``reference`` is the designator of this *placement* of the symbol.  The
    walk is per sheet placement, not per sheet file (issue #6004): a sheet
    file placed twice yields its symbols twice, each under the reference its
    ``(instances (project ... (path "/<root>/<sheet-uuid>" (reference ...))))``
    entry records for that placement (``R1`` under ``MCU_A``, ``R11`` under
    ``MCU_B``) -- the same resolution board LVS uses (issue #5815).

    ``placement`` is the symbol's raw position, rotation and mirror axis
    (``None`` when the sheet has no no-connect flags, so nothing needs it);
    ``no_connect_zone`` is the sheet's :class:`_NoConnectZone` or ``None``.
    Both are properties of the sheet *file*, so they are read once per file
    and shared by its placements.
    """
    from kicad_tools.lvs.board_lvs import _iter_instance_symbols

    geometry: dict[Path, tuple[dict[str, tuple[float, float, float]], _NoConnectZone | None]] = {}
    for ref, sym, visit in _iter_instance_symbols(sch_path):
        source = visit.source.resolve()
        if source not in geometry:
            geometry[source] = (
                _read_sheet_geometry(visit.source) if visit.schematic.no_connects else ({}, None)
            )
        raw, zone = geometry[source]
        placement = None
        if zone is not None:
            x, y, rotation = raw.get(
                getattr(sym, "uuid_str", ""),
                (sym.x, sym.y, getattr(sym, "rotation", 0) or 0),
            )
            placement = _Placement(x, y, rotation, getattr(sym, "mirror", "") or "")
        yield ref, sym, placement, zone


def _pinfunction_for(name: str) -> str:
    # KiCad omits ``pinfunction`` for unnamed pins (name "~" or empty).
    return "" if name in ("", "~") else name


def _placed_pin_xy(placement: _Placement, pin) -> _Point:
    """Schematic (Y-down) position of *pin*'s connection point on a placed symbol.

    Delegates to the shared rotate-then-mirror transform
    (:func:`kicad_tools.core.symbol_transform.symbol_to_sheet_offset`), the
    same one :meth:`SymbolInstance.pin_position` uses; unlike that method the
    result is not rounded, since *placement* keeps the raw coordinates.
    """
    from kicad_tools.core.symbol_transform import symbol_to_sheet_offset

    x, y = pin.connection_point()
    dx, dy = symbol_to_sheet_offset(x, y, placement.rotation, placement.mirror)
    return (placement.x + dx, placement.y + dy)


def _pin_has_no_connect_flag(
    sym, pin, placement: _Placement | None, zone: _NoConnectZone | None
) -> bool:
    """Whether a no-connect flag reaches *pin* of the placed symbol *sym*."""
    if zone is None or placement is None:
        return False
    # Only the placed unit's pins (and unit-0 commons) have a real position.
    pin_unit = getattr(pin, "unit", 0)
    if pin_unit not in (0, getattr(sym, "unit", 1)):
        return False
    try:
        return zone.covers(*_placed_pin_xy(placement, pin))
    except Exception:  # pragma: no cover - defensive: odd library geometry
        return False


def _merge(old: PadPinInfo | None, new: PadPinInfo) -> PadPinInfo:
    """Combine two schematic pins that land on one key (stacked/multi-unit)."""
    if old is None:
        return new
    flagged = old.no_connect or new.no_connect
    if old.pintype == "passive" and new.pintype != "passive":
        return PadPinInfo(new.pinfunction, new.pintype, flagged)
    return PadPinInfo(old.pinfunction, old.pintype, flagged)


def schematic_pin_info(sch_path: str | Path) -> dict[tuple[str, str], PadPinInfo]:
    """Map ``(reference, schematic pin number)`` to the pin's name and type.

    Walks the full sheet hierarchy and reads each symbol's embedded library
    definition.  Power symbols (``#PWR``/``#FLG`` references) never reach the
    PCB and are skipped.  When a symbol defines several pins with the same
    number (stacked pins), the first non-``passive`` type wins so a stacked
    ``power_in`` group is not masked by a passive twin.  ``no_connect`` is set
    when a no-connect flag reaches the pin (see :class:`_NoConnectZone`).

    A unit-0 (common) pin of a multi-unit symbol is drawn once per placed
    unit, and KiCad's netlist lists one node per copy.  The pad takes the
    first copy in KiCad's net order, which for unconnected copies is the
    lowest unit (``unconnected-(U1A-…)`` sorts before ``unconnected-(U1B-…)``),
    so only that copy's flag counts -- not an OR across units.
    """
    info: dict[tuple[str, str], PadPinInfo] = {}
    # (ref, pin) -> (lowest placed unit seen, that copy's flag) for unit-0 pins.
    common_flags: dict[tuple[str, str], tuple[int, bool]] = {}
    for ref, sym, placement, zone in _iter_schematic_symbols(Path(sch_path)):
        if not ref or ref.startswith("#"):
            continue
        symbol_def = getattr(sym, "symbol_def", None)
        if symbol_def is None:
            continue
        sym_unit = getattr(sym, "unit", 1) or 1
        unit_flags: dict[str, bool] = {}
        for pin in symbol_def.pins:
            if not pin.number:
                continue
            key = (ref, pin.number)
            flagged = _pin_has_no_connect_flag(sym, pin, placement, zone)
            if getattr(pin, "unit", 0) == 0:
                # Stacked unit-0 pins within this placement still OR together.
                unit_flags[pin.number] = unit_flags.get(pin.number, False) or flagged
                flagged = False
            new = PadPinInfo(_pinfunction_for(pin.name), pin.pin_type or "passive", flagged)
            info[key] = _merge(info.get(key), new)
        for number, flagged in unit_flags.items():
            key = (ref, number)
            seen = common_flags.get(key)
            if seen is None or sym_unit < seen[0]:
                common_flags[key] = (sym_unit, flagged)
    for key, (_, flagged) in common_flags.items():
        if flagged and key in info:
            old = info[key]
            info[key] = PadPinInfo(old.pinfunction, old.pintype, True)
    return info


def pad_pin_info_map(sch_path: str | Path, pcb: PCB) -> dict[tuple[str, str], PadPinInfo]:
    """Map ``(reference, pad number)`` to the schematic pin data for that pad.

    Schematic pin numbers are translated to footprint pad numbers with
    :func:`kicad_tools.operations.netlist.build_pin_to_pad_map`, the same
    resolution ``kct pcb sync-netlist`` uses for net assignment.
    """
    from kicad_tools.operations.netlist import build_pin_to_pad_map

    pin_info = schematic_pin_info(sch_path)
    try:
        pin_to_pad = build_pin_to_pad_map(sch_path, pcb)
    except Exception as exc:  # pragma: no cover - defensive, mirrors sync-netlist
        logger.warning("pin-to-pad map failed, assuming identity: %s", exc)
        pin_to_pad = {}

    result: dict[tuple[str, str], PadPinInfo] = {}
    for (ref, pin), data in pin_info.items():
        pad = pin_to_pad.get((ref, pin), pin)
        key = (ref, pad)
        result[key] = _merge(result.get(key), data)
    return result


# ---------------------------------------------------------------------------
# Object-level annotation (PCB + S-expression tree)
# ---------------------------------------------------------------------------


def annotate_pcb_pintypes(
    pcb: PCB,
    sch_path: str | Path,
    pad_info: dict[tuple[str, str], PadPinInfo] | None = None,
) -> PinTypeAnnotation:
    """Write ``pinfunction``/``pintype`` onto every pad of *pcb* from *sch_path*.

    Updates both ``Pad.pintype`` and the pad's S-expression node so the change
    reaches :meth:`PCB.save`.  Pads with no schematic pin (mounting holes,
    footprints with no symbol) are left untouched.
    """
    from kicad_tools.sexp import SExp

    if pad_info is None:
        pad_info = pad_pin_info_map(sch_path, pcb)
    result = PinTypeAnnotation()
    seen: set[tuple[str, str]] = set()

    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue
        for pad in fp.pads:
            data = pad_info.get((ref, pad.number))
            if data is None:
                continue
            seen.add((ref, pad.number))
            pintype = data.effective_pintype
            node = pad._sexp_node
            current_func = ""
            if node is not None and (fn := node.find_child("pinfunction")) is not None:
                current_func = fn.get_string(0) or ""
            if pad.pintype == pintype and current_func == data.pinfunction:
                result.unchanged += 1
                continue
            pad.pintype = pintype
            result.updated += 1
            if node is None:
                continue
            for name in ("pinfunction", "pintype"):
                while (old := node.find_child(name)) is not None:
                    node.remove(old)
            new_children = []
            if data.pinfunction:
                new_children.append(SExp.list("pinfunction", SExp.quoted_atom(data.pinfunction)))
            new_children.append(SExp.list("pintype", SExp.quoted_atom(pintype)))
            net_node = node.find_child("net")
            if net_node is not None:
                idx = next(i for i, c in enumerate(node.children) if c is net_node) + 1
                for offset, child in enumerate(new_children):
                    node.insert(idx + offset, child)
            else:
                for child in new_children:
                    node.append(child)

    result.missing_pads = sorted(
        f"{ref}.{pad}" for ref, pad in pad_info.keys() - seen if pcb.get_footprint(ref) is not None
    )
    return result


# ---------------------------------------------------------------------------
# Text-preserving file annotation
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    """A parenthesised list in the source text: ``text[start:end]``."""

    start: int
    end: int
    name: str
    children: list[_Span] = field(default_factory=list)


_NAME_RE = re.compile(r"\(\s*([^\s()\"]+)")


def _scan_spans(text: str) -> _Span:
    """Build a list-span tree for *text* (string- and escape-aware)."""
    root = _Span(0, len(text), "")
    stack: list[_Span] = [root]
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            m = _NAME_RE.match(text, i)
            span = _Span(i, -1, m.group(1) if m else "")
            stack[-1].children.append(span)
            stack.append(span)
        elif c == ")":
            if len(stack) > 1:
                span = stack.pop()
                span.end = i + 1
        i += 1
    return root


_REF_PROPERTY_RE = re.compile(r'\(\s*property\s+"Reference"\s+"((?:[^"\\]|\\.)*)"')
_REF_FPTEXT_RE = re.compile(r'\(\s*fp_text\s+reference\s+"?((?:[^"\\\s)]|\\.)*)"?')
_PAD_NUMBER_RE = re.compile(r'\(\s*pad\s+(?:"((?:[^"\\]|\\.)*)"|([^\s()"]+))')


def _footprint_reference(text: str, fp: _Span) -> str:
    for child in fp.children:
        if child.name == "property":
            if m := _REF_PROPERTY_RE.match(text, child.start):
                return m.group(1)
        elif child.name == "fp_text":
            if m := _REF_FPTEXT_RE.match(text, child.start):
                return m.group(1)
    return ""


def _child_separator(text: str, anchor: int) -> str:
    """Whitespace to put before a new sibling of the child starting at *anchor*.

    Multi-line pads (one child per line, as KiCad writes them) get a newline
    plus the anchor child's indentation; single-line pads get a space.
    """
    i = anchor
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i > 0 and text[i - 1] == "\n":
        return "\n" + text[i:anchor]
    return " "


def _quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _iter_footprint_spans(root: _Span):
    for top in root.children:  # the (kicad_pcb ...) list
        for child in top.children:
            if child.name in ("footprint", "module"):
                yield child


def annotate_pcb_file_pintypes(
    pcb_path: str | Path,
    sch_path: str | Path,
    *,
    dry_run: bool = False,
) -> PinTypeAnnotation:
    """Annotate pads in *pcb_path* with schematic pin data, preserving its text.

    Only the ``(pinfunction ...)``/``(pintype ...)`` children of affected pads
    change; every other byte of the file is kept, so hand-formatted generator
    output does not churn.  The new children are inserted directly after the
    pad's ``(net ...)`` child -- where KiCad writes them -- or before the
    pad's closing paren when it has no net.

    Args:
        pcb_path: ``.kicad_pcb`` file to rewrite.
        sch_path: Root ``.kicad_sch`` of the design.
        dry_run: Compute the result without writing the file.
    """
    from kicad_tools.schema.pcb import PCB

    pcb_path = Path(pcb_path)
    text = pcb_path.read_text(encoding="utf-8")
    pad_info = pad_pin_info_map(sch_path, PCB.load(pcb_path))

    result = PinTypeAnnotation()
    seen: set[tuple[str, str]] = set()
    refs_on_board: set[str] = set()
    # (start, end, replacement) edits, applied back-to-front.
    edits: list[tuple[int, int, str]] = []

    for fp in _iter_footprint_spans(_scan_spans(text)):
        ref = _footprint_reference(text, fp)
        if not ref:
            continue
        refs_on_board.add(ref)
        for pad in fp.children:
            if pad.name != "pad":
                continue
            m = _PAD_NUMBER_RE.match(text, pad.start)
            if not m:
                continue
            number = m.group(1) if m.group(1) is not None else m.group(2)
            data = pad_info.get((ref, number))
            if data is None:
                continue
            seen.add((ref, number))

            net_span = next((c for c in pad.children if c.name == "net"), None)
            pintype = data.effective_pintype

            existing = {c.name: c for c in pad.children if c.name in ("pinfunction", "pintype")}
            want = {"pintype": f"(pintype {_quote(pintype)})"}
            if data.pinfunction:
                want["pinfunction"] = f"(pinfunction {_quote(data.pinfunction)})"
            have = {k: text[s.start : s.end] for k, s in existing.items()}
            if have == want:
                result.unchanged += 1
                continue
            result.updated += 1

            # Drop stale children together with the separator in front of
            # them: the preceding spaces/tabs and, on a multi-line pad, the
            # line break too, so the old child's whole line disappears and
            # the insertion below (which brings its own separator) does not
            # leave a blank line behind.
            for span in existing.values():
                start = span.start
                while start > pad.start and text[start - 1] in " \t":
                    start -= 1
                if start > pad.start and text[start - 1] == "\n":
                    start -= 1
                    if start > pad.start and text[start - 1] == "\r":
                        start -= 1
                edits.append((start, span.end, ""))
            if net_span is not None:
                at = net_span.end
                anchor = net_span.start
            else:
                at = pad.end - 1
                while at > pad.start and text[at - 1] in " \t\r\n":
                    at -= 1
                anchor = pad.children[-1].start if pad.children else at
            sep = _child_separator(text, anchor)
            insertion = "".join(sep + want[k] for k in ("pinfunction", "pintype") if k in want)
            edits.append((at, at, insertion))

    result.missing_pads = sorted(
        f"{ref}.{pad}" for ref, pad in pad_info.keys() - seen if ref in refs_on_board
    )

    if edits and not dry_run:
        # Edits never overlap; an insertion and a deletion may share a start
        # offset, so order by (start, end): the zero-width insertion first.
        edits.sort(key=lambda e: (e[0], e[1]))
        pieces: list[str] = []
        cursor = 0
        for start, end, repl in edits:
            pieces.append(text[cursor:start])
            pieces.append(repl)
            cursor = max(cursor, end)
        pieces.append(text[cursor:])
        pcb_path.write_text("".join(pieces), encoding="utf-8")
    return result
