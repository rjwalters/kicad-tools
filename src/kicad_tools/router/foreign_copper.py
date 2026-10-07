"""Other nets' copper already on a board, for ``kct route-auto`` (Issue #6107).

``kct route-auto`` (the :class:`~kicad_tools.router.orchestrator.RoutingOrchestrator`)
must never write copper that KiCad's DRC flags as a short or a clearance
violation against another net.  Issue #6001 added an output gate that refused
copper *overlapping* another net's straight tracks and vias.  It missed three
cases, each reproduced with ``kicad-cli pcb drc``:

* other nets' **pads** (``shorting_items`` on the output);
* **arcs**, which live in ``pcb.arcs``, not ``pcb.segments``;
* **clearance**: copper 0.05 mm from another net's track does not overlap it,
  but KiCad reports a ``clearance`` violation for it.

This module is the geometry behind the extended gate.  It reads every piece of
copper on the board -- tracks, arcs, vias and pads, with each pad's real shape
-- and measures new copper against it with the shared clearance kernel
(:func:`~kicad_tools.router.clearance_kernel.copper_gap`, the same exact pad
model ``kct check`` uses).

Which clearance
---------------
The requirement is resolved **per item pair**, as KiCad's DRC does it
(:mod:`~kicad_tools.router.board_clearance_rules`, issue #6122): the last
matching ``.kicad_dru`` rule wins, else the larger of the two nets' netclass
clearances floored by the ``.kicad_pro`` board minimum.  A rule whose
condition cannot be evaluated is taken conservatively (the larger value).
:func:`board_required_clearance` is the board-wide figure -- two unassigned
``Default``-class tracks -- that messages quote.  A board with no project
gets KiCad's built-in ``Default`` clearance, 0.2 mm.

Holes (issue #6139)
-------------------
Every drilled hole -- an NPTH hole or slot, a plated pad's drill, a via's
drill -- is also a gate item (:func:`board_holes`).  New copper is measured
against the hole's real outline (round or oval) at the board's
``hole_clearance``, and against an **NPTH slot** also at its
``edge_clearance``, because KiCad treats a non-plated slot as board edge.
A new via's own drill is measured against other nets' copper at
``hole_clearance`` too.  A bare NPTH hole still contributes no *copper*.
Copper that **crosses** a hole is always refused, even where an ``ignore``
rule switches ``hole_clearance`` off: the drill would sever it.

Hole to hole, physical clearance (issue #6150)
----------------------------------------------
A new via's drill is measured against **every** drilled hole on the board,
the routed net's own included, and against the other new vias' drills, at
``hole_to_hole`` (KiCad checks that constraint regardless of net).  New
copper is also measured at ``physical_clearance`` against other nets' copper
and holes, and at ``physical_hole_clearance`` against their holes (and a new
via's drill against their copper).  Those two constraints exist only as
``.kicad_dru`` rules.  KiCad applies them within a net too, which the gate
does not: a rule that reaches the routed net's own pads is broken by every
connection to them, so no route could pass it.

The routed net's own copper is exempt.  Unassigned (net 0) tracks, vias and
arcs are exempt too: KiCad reassigns floating copper through connectivity on
load.  Unassigned **pads** are not -- KiCad reports copper across a no-connect
pin as a short -- except an NPTH hole with no annular copper, which KiCad
measures as a hole, not as copper.

Frames
------
Everything here is in the schema ``PCB``'s board-relative frame, which is the
frame the orchestrator routes in.  :func:`describe_point` adds the board
origin back so messages quote the coordinates KiCad shows.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .board_clearance_rules import (
    EDGE_CLEARANCE,
    HOLE_CLEARANCE,
    HOLE_TO_HOLE,
    KICAD_DEFAULT_CLEARANCE_MM,
    KICAD_DEFAULT_EDGE_CLEARANCE_MM,
    KICAD_DEFAULT_HOLE_CLEARANCE_MM,
    KICAD_DEFAULT_HOLE_TO_HOLE_MM,
    PHYSICAL_CLEARANCE,
    PHYSICAL_HOLE_CLEARANCE,
    BoardClearanceRules,
    ItemProps,
    item_props_for_type,
)
from .clearance_kernel import (
    ALL_LAYERS,
    CLEARANCE_EPSILON_MM,
    KPad,
    KSegment,
    KShape,
    KVia,
    copper_gap,
    make_pad,
)

#: Sagitta bound when an arc is flattened for the gate or the routing grid.
#: The flattened copper is widened by twice this, so it always covers the arc.
ARC_FLATTEN_ERROR_MM = 0.001

_BUCKET_MM = 2.0


@dataclass(frozen=True)
class ForeignItem:
    """One piece of copper already on the board.

    Attributes:
        kind: ``"track"``, ``"arc"``, ``"via"``, ``"pad"`` or ``"hole"`` (a
            drilled hole; its ``shapes`` are the hole's outline, not copper).
        net: Net number (0 for unassigned copper).
        net_name: Net name, ``""`` when the board gives none.
        label: Human-readable identity, e.g. ``pad R5.1`` or
            ``track (102.000, 108.000)-(128.000, 108.000) on F.Cu``.
        shapes: Kernel copper.  An arc is several flattened segments.
        layers: Copper layer names the item is on; ``None`` means every
            copper layer (vias, through-hole pads, ``*.Cu`` pads).
        bbox: ``(min_x, min_y, max_x, max_y)`` of the copper itself.
        owner: For a hole, ``"pad"`` or ``"via"`` (what it is drilled in).
        plated: For a hole, whether it is plated (KiCad's ``isPlated()``).
        slot_edge: An NPTH slot, which KiCad also measures as board edge.
        parent: For a hole, the label of the pad or via it is drilled in.
        pad_type: For a pad (or a pad's hole), the schema pad ``type``
            (``smd``, ``thru_hole``, ``np_thru_hole``, ...).
    """

    kind: str
    net: int
    net_name: str
    label: str
    shapes: tuple[KShape, ...]
    layers: frozenset[str] | None
    bbox: tuple[float, float, float, float]
    owner: str = ""
    plated: bool | None = None
    slot_edge: bool = False
    parent: str = ""
    pad_type: str = ""

    def props(self) -> ItemProps:
        """This item as a ``.kicad_dru`` condition sees it."""
        if self.net == 0 and not self.net_name:
            name: str | None = ""
        else:
            name = self.net_name or None
        kind = self.owner if self.kind == "hole" else self.kind
        plated = self.plated
        if plated is None and kind == "via":
            plated = True
        if plated is None and kind == "pad":
            plated = {"thru_hole": True, "np_thru_hole": False, "smd": False}.get(self.pad_type)
        layer = (
            next(iter(self.layers)) if self.layers is not None and len(self.layers) == 1 else None
        )
        return item_props_for_type(kind, name, plated, self.pad_type or None, layer)

    @property
    def net_label(self) -> str:
        if self.net_name:
            return self.net_name
        return f"Net_{self.net}" if self.net else "<no net>"


@dataclass(frozen=True)
class CopperConflict:
    """New copper that shorts, or is too close to, another net's copper or a hole."""

    #: "short", "clearance", "hole_clearance", "edge_clearance", "hole_to_hole",
    #: "physical_clearance" or "physical_hole_clearance"
    kind: str
    new_item: str  # "segment" or "via"
    other: ForeignItem
    gap_mm: float
    required_mm: float


@dataclass
class ConflictReport:
    """Every conflict the gate found in one strategy's output."""

    required_mm: float
    conflicts: list[CopperConflict] = field(default_factory=list)
    segments_in_conflict: int = 0
    vias_in_conflict: int = 0

    @property
    def shorts(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "short"]

    @property
    def clearance(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "clearance"]

    @property
    def hole_clearance(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "hole_clearance"]

    @property
    def edge_clearance(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "edge_clearance"]

    @property
    def hole_to_hole(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "hole_to_hole"]

    @property
    def physical_clearance(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "physical_clearance"]

    @property
    def physical_hole_clearance(self) -> list[CopperConflict]:
        return [c for c in self.conflicts if c.kind == "physical_hole_clearance"]

    def __bool__(self) -> bool:
        return bool(self.conflicts)


# ---------------------------------------------------------------------------
# Reading the board
# ---------------------------------------------------------------------------


def describe_point(x: float, y: float, origin: tuple[float, float]) -> str:
    """``(x, y)`` in the sheet frame KiCad displays, to 3 decimals."""
    return f"({x + origin[0]:.3f}, {y + origin[1]:.3f})"


def _copper_layers(names: Iterable[str]) -> frozenset[str] | None:
    """A pad's copper layers; ``None`` for every layer."""
    layers: set[str] = set()
    for name in names:
        if name == "*.Cu":
            return None
        if name == "F&B.Cu":
            layers.update(("F.Cu", "B.Cu"))
        elif name.endswith(".Cu"):
            layers.add(name)
    return frozenset(layers)


def _npth_is_bare(pad: Any, w: float, h: float) -> bool:
    """Whether an NPTH pad's drill covers its whole pad, leaving no copper.

    A round drill is ``(drill D)``; an oval slot is ``(drill oval DX DY)``,
    whose dimensions live in ``drill_size`` (``drill`` is then 0).
    """
    slot = getattr(pad, "drill_size", None)
    if slot:
        dx, dy = float(slot[0] or 0.0), float(slot[1] or 0.0)
        return dx > 0 and dy > 0 and w <= dx and h <= dy
    drill = float(getattr(pad, "drill", 0.0) or 0.0)
    return drill > 0 and max(w, h) <= drill


def _shape_bbox(shape: KShape) -> tuple[float, float, float, float]:
    if isinstance(shape, KSegment):
        h = shape.width / 2.0
        return (
            min(shape.x1, shape.x2) - h,
            min(shape.y1, shape.y2) - h,
            max(shape.x1, shape.x2) + h,
            max(shape.y1, shape.y2) + h,
        )
    if isinstance(shape, KVia):
        r = shape.diameter / 2.0
        return (shape.x - r, shape.y - r, shape.x + r, shape.y + r)
    if isinstance(shape, KPad):
        r = shape.corner_radius
        xs = [p[0] for p in shape.core] or [shape.cx]
        ys = [p[1] for p in shape.core] or [shape.cy]
        return (min(xs) - r, min(ys) - r, max(xs) + r, max(ys) + r)
    raise TypeError(f"unsupported shape {type(shape).__name__}")


def _union_bbox(boxes: Iterable[tuple[float, float, float, float]]) -> tuple[float, ...]:
    boxes = list(boxes)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def flatten_arc(arc: Any, max_error_mm: float = ARC_FLATTEN_ERROR_MM) -> list[tuple[float, float]]:
    """An arc's centreline as a polyline whose chords stay within ``max_error_mm``.

    Uses the schema ``Arc.centerline_points`` (the arc's true circle through
    start, mid and end -- never its chord).
    """
    return list(arc.centerline_points(max_error_mm))


def board_copper(pcb: Any) -> list[ForeignItem]:
    """Every track, arc, via and pad on a schema ``PCB``, as gate items.

    Items that cannot be read (a malformed arc, a pad with no size) are
    skipped; the lightweight test mocks, which expose none of these lists,
    give an empty list.
    """
    origin = tuple(getattr(pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0))
    items: list[ForeignItem] = []

    segments = getattr(pcb, "segments", None)
    if isinstance(segments, list):
        for s in segments:
            try:
                (x1, y1), (x2, y2) = s.start, s.end
                track = KSegment(float(x1), float(y1), float(x2), float(y2), float(s.width))
                layer = str(s.layer)
            except (AttributeError, TypeError, ValueError):
                continue
            items.append(
                ForeignItem(
                    kind="track",
                    net=int(getattr(s, "net_number", 0) or 0),
                    net_name=str(getattr(s, "net_name", "") or ""),
                    label=(
                        f"track {describe_point(x1, y1, origin)}-"
                        f"{describe_point(x2, y2, origin)} on {layer}"
                    ),
                    shapes=(track,),
                    layers=frozenset((layer,)),
                    bbox=_shape_bbox(track),
                )
            )

    arcs = getattr(pcb, "arcs", None)
    if isinstance(arcs, list):
        for a in arcs:
            try:
                points = flatten_arc(a)
                width = float(a.width) + 2 * ARC_FLATTEN_ERROR_MM
                layer = str(a.layer)
                shapes = tuple(
                    KSegment(p[0], p[1], q[0], q[1], width)
                    for p, q in zip(points, points[1:], strict=False)
                )
                (sx, sy), (mx, my), (ex, ey) = a.start, a.mid, a.end
            except (AttributeError, TypeError, ValueError):
                continue
            if not shapes:
                continue
            items.append(
                ForeignItem(
                    kind="arc",
                    net=int(getattr(a, "net_number", 0) or 0),
                    net_name=str(getattr(a, "net_name", "") or ""),
                    label=(
                        f"arc {describe_point(sx, sy, origin)} via "
                        f"{describe_point(mx, my, origin)} to "
                        f"{describe_point(ex, ey, origin)} on {layer}"
                    ),
                    shapes=shapes,
                    layers=frozenset((layer,)),
                    bbox=tuple(_union_bbox(_shape_bbox(s) for s in shapes)),  # type: ignore[arg-type]
                )
            )

    vias = getattr(pcb, "vias", None)
    if isinstance(vias, list):
        for v in vias:
            try:
                x, y = v.position
                via_copper = KVia(
                    float(x), float(y), float(v.size), float(getattr(v, "drill", 0.0))
                )
            except (AttributeError, TypeError, ValueError):
                continue
            items.append(
                ForeignItem(
                    kind="via",
                    net=int(getattr(v, "net_number", 0) or 0),
                    net_name=str(getattr(v, "net_name", "") or ""),
                    label=f"via at {describe_point(x, y, origin)}",
                    shapes=(via_copper,),
                    layers=None,
                    bbox=_shape_bbox(via_copper),
                )
            )

    footprints = getattr(pcb, "footprints", None)
    if isinstance(footprints, list):
        from kicad_tools.validate.clearance_shapes import pad_center

        for fp in footprints:
            for pad in getattr(fp, "pads", None) or []:
                try:
                    layers = _copper_layers(getattr(pad, "layers", None) or [])
                    if layers is not None and not layers:
                        continue  # no copper (e.g. a paste-only aperture)
                    w, h = float(pad.size[0]), float(pad.size[1])
                    if w <= 0 or h <= 0:
                        continue
                    if str(getattr(pad, "type", "")) == "np_thru_hole" and _npth_is_bare(pad, w, h):
                        continue  # a bare NPTH hole carries no copper
                    cx, cy = pad_center(pad, fp)
                    pad_copper = make_pad(
                        str(pad.shape),
                        w,
                        h,
                        float(getattr(pad, "roundrect_rratio", 0.25) or 0.25),
                        float(getattr(pad, "rotation", 0.0) or 0.0),
                        cx,
                        cy,
                        ALL_LAYERS,
                        float(getattr(pad, "drill", 0.0) or 0.0),
                    )
                except (AttributeError, TypeError, ValueError, IndexError):
                    continue
                if not pad_copper.core:
                    continue
                ref = str(getattr(fp, "reference", "") or "?")
                items.append(
                    ForeignItem(
                        kind="pad",
                        net=int(getattr(pad, "net_number", 0) or 0),
                        net_name=str(getattr(pad, "net_name", "") or ""),
                        label=f"pad {ref}.{pad.number} at {describe_point(cx, cy, origin)}",
                        shapes=(pad_copper,),
                        layers=layers,
                        bbox=_shape_bbox(pad_copper),
                        pad_type=str(getattr(pad, "type", "") or ""),
                    )
                )
    return items


def _pad_hole(pad: Any, fp: Any) -> tuple[KPad, bool] | None:
    """A drilled pad's hole outline as a kernel region, plus whether it is a slot.

    ``(drill D)`` is a disc; ``(drill oval DX DY)`` an oval in the pad's own
    frame, rotated with the pad.  ``(offset ...)`` moves the hole off the pad
    centre.  ``None`` for an undrilled (SMD) pad.
    """
    from kicad_tools.core.geometry import rotate_pad_offset
    from kicad_tools.validate.clearance_shapes import pad_center

    rotation = float(getattr(pad, "rotation", 0.0) or 0.0)
    slot = getattr(pad, "drill_size", None)
    if slot:
        dx, dy = float(slot[0] or 0.0), float(slot[1] or 0.0)
        shape, is_slot = "oval", True
    else:
        dx = dy = float(getattr(pad, "drill", 0.0) or 0.0)
        shape, is_slot = "circle", False
    if dx <= 0 or dy <= 0:
        return None
    cx, cy = pad_center(pad, fp)
    off = getattr(pad, "drill_offset", None) or (0.0, 0.0)
    ox, oy = float(off[0]), float(off[1])
    if math.isfinite(ox) and math.isfinite(oy) and (ox or oy):
        rx, ry = rotate_pad_offset(ox, oy, rotation)
        cx, cy = cx + rx, cy + ry
    return make_pad(shape, dx, dy, 0.0, rotation, cx, cy, ALL_LAYERS, 0.0), is_slot


def board_holes(pcb: Any) -> list[ForeignItem]:
    """Every drilled hole on a schema ``PCB`` -- pad drills and via drills -- as
    gate items of kind ``"hole"`` (issue #6139).

    The item's shape is the hole's own outline (a disc, or an oval for a
    ``(drill oval ...)`` slot), measured at the board's ``hole_clearance``.
    A non-plated slot is flagged :attr:`ForeignItem.slot_edge`: KiCad also
    measures it at ``edge_clearance``, as board edge.
    """
    origin = tuple(getattr(pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0))
    items: list[ForeignItem] = []

    footprints = getattr(pcb, "footprints", None)
    if isinstance(footprints, list):
        for fp in footprints:
            ref = str(getattr(fp, "reference", "") or "?")
            for pad in getattr(fp, "pads", None) or []:
                pad_type = str(getattr(pad, "type", ""))
                if pad_type not in ("thru_hole", "np_thru_hole"):
                    continue
                try:
                    hole = _pad_hole(pad, fp)
                except (AttributeError, TypeError, ValueError, IndexError):
                    continue
                if hole is None or not hole[0].core:
                    continue
                region, is_slot = hole
                plated = pad_type == "thru_hole"
                number = str(getattr(pad, "number", "") or "")
                pad_id = f"{ref}.{number}" if number else ref
                where = describe_point(region.cx, region.cy, origin)
                what = (
                    ("slot" if is_slot else "hole")
                    if plated
                    else ("NPTH slot" if is_slot else "NPTH hole")
                )
                items.append(
                    ForeignItem(
                        kind="hole",
                        net=int(getattr(pad, "net_number", 0) or 0),
                        net_name=str(getattr(pad, "net_name", "") or ""),
                        label=f"{what} of pad {pad_id} at {where}",
                        shapes=(region,),
                        layers=None,
                        bbox=_shape_bbox(region),
                        owner="pad",
                        plated=plated,
                        slot_edge=is_slot and not plated,
                        pad_type=pad_type,
                        parent=f"pad {ref}.{pad.number} at {where}",  # matches board_copper
                    )
                )

    vias = getattr(pcb, "vias", None)
    if isinstance(vias, list):
        for v in vias:
            try:
                x, y = v.position
                drill = float(getattr(v, "drill", 0.0) or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue
            if drill <= 0:
                continue
            via_hole = KVia(float(x), float(y), drill)
            where = describe_point(x, y, origin)
            items.append(
                ForeignItem(
                    kind="hole",
                    net=int(getattr(v, "net_number", 0) or 0),
                    net_name=str(getattr(v, "net_name", "") or ""),
                    label=f"drill of via at {where}",
                    shapes=(via_hole,),
                    layers=None,
                    bbox=_shape_bbox(via_hole),
                    owner="via",
                    plated=True,
                    parent=f"via at {where}",
                )
            )
    return items


# ---------------------------------------------------------------------------
# The clearance requirement
# ---------------------------------------------------------------------------


def board_clearance_rules(pcb_path: str | Path | None) -> BoardClearanceRules | None:
    """The board's per-pair clearance rules (issue #6122); ``None`` without a file."""
    if pcb_path is None:
        return None
    return BoardClearanceRules.from_board(pcb_path)


def board_required_clearance(pcb_path: str | Path | None, fallback_mm: float) -> float:
    """The board-wide copper clearance KiCad's DRC applies, in mm.

    Two unassigned (``Default``-class) tracks: the last matching unconditional
    ``.kicad_dru`` rule, else the ``Default`` netclass floored by the board
    minimum (KiCad's 0.2 mm when the project declares no ``Default``).  A rule
    whose condition cannot be evaluated counts at its larger reading.
    ``fallback_mm`` (the router's own clearance) when there is no file.
    Per-pair values -- an ``HV`` class, a conditional rule -- come from
    :func:`board_clearance_rules`.
    """
    rules = board_clearance_rules(pcb_path)
    if rules is None:
        return fallback_mm
    return rules.board_clearance()


# ---------------------------------------------------------------------------
# Measuring new copper against it
# ---------------------------------------------------------------------------


class _Index:
    """Uniform-bucket index over item bounding boxes."""

    def __init__(self, items: Sequence[ForeignItem], margin: float):
        self.items = items
        self.margin = margin
        self.buckets: dict[tuple[int, int], list[int]] = {}
        for i, item in enumerate(items):
            for key in self._keys(item.bbox, margin):
                self.buckets.setdefault(key, []).append(i)

    @staticmethod
    def _keys(bbox: Sequence[float], margin: float) -> Iterable[tuple[int, int]]:
        x0 = math.floor((bbox[0] - margin) / _BUCKET_MM)
        y0 = math.floor((bbox[1] - margin) / _BUCKET_MM)
        x1 = math.floor((bbox[2] + margin) / _BUCKET_MM)
        y1 = math.floor((bbox[3] + margin) / _BUCKET_MM)
        for bx in range(x0, x1 + 1):
            for by in range(y0, y1 + 1):
                yield (bx, by)

    def near(self, bbox: Sequence[float]) -> list[ForeignItem]:
        seen: set[int] = set()
        out: list[ForeignItem] = []
        m = self.margin
        for key in self._keys(bbox, 0.0):
            for i in self.buckets.get(key, ()):
                if i in seen:
                    continue
                seen.add(i)
                ob = self.items[i].bbox
                if (
                    ob[0] - m <= bbox[2]
                    and bbox[0] <= ob[2] + m
                    and ob[1] - m <= bbox[3]
                    and bbox[1] <= ob[3] + m
                ):
                    out.append(self.items[i])
        return out


def _layer_name(layer: Any) -> str | None:
    name = getattr(layer, "kicad_name", None)
    if name is None and isinstance(layer, str):
        name = layer
    return str(name) if name else None


_HOLE_KINDS = frozenset({HOLE_CLEARANCE, PHYSICAL_HOLE_CLEARANCE, EDGE_CLEARANCE, HOLE_TO_HOLE})


def find_conflicts(
    items: Sequence[ForeignItem],
    segments: Sequence[Any],
    vias: Sequence[Any],
    *,
    own_ids: set[int],
    own_names: set[str],
    required_mm: float,
    rules: BoardClearanceRules | None = None,
    net_name: str | None = None,
    origin: tuple[float, float] = (0.0, 0.0),
) -> ConflictReport:
    """New router copper that shorts, or crowds, another net's copper or a hole.

    ``segments`` / ``vias`` are router primitives (``x1``/``y1``/``x2``/``y2``
    + ``width`` + ``layer``; ``x``/``y`` + ``diameter`` + ``drill``).  A via is
    treated as a through via (on every layer), which errs toward refusing.
    Copper of the routed net (``own_ids`` / ``own_names``) and net-0 tracks,
    vias and arcs are exempt; net-0 pads are not.

    ``items`` may mix copper (:func:`board_copper`) and holes
    (:func:`board_holes`).  New copper is measured against copper at the
    pair's ``clearance`` and ``physical_clearance``, against a hole at its
    ``hole_clearance``, ``physical_hole_clearance`` and ``physical_clearance``,
    and against an NPTH slot also at its ``edge_clearance``; a new via's drill
    is measured against other nets' copper at ``hole_clearance`` and
    ``physical_hole_clearance``, and against every hole -- the routed net's
    own and the other new vias' included -- at ``hole_to_hole`` (#6150).

    With ``rules`` every requirement is resolved per pair, with ``net_name``
    (the routed net; ``None`` when unknown) as the new copper's net (issue
    #6122).  Without, copper uses ``required_mm``, holes KiCad's defaults,
    and the physical constraints (``.kicad_dru``-only) are not checked.
    ``origin`` is the board origin, for the labels of new vias.
    """
    report = ConflictReport(required_mm=required_mm)

    def _foreign(item: ForeignItem) -> bool:
        if item.net_name and item.net_name in own_names:
            return False
        if item.kind in ("pad", "hole") and item.owner != "via":
            # Pads and pad holes: net-0 ones (no-connect pins, NPTH holes) count.
            return item.net == 0 or item.net not in own_ids
        return item.net > 0 and item.net not in own_ids

    others = [i for i in items if _foreign(i)]
    # hole_to_hole is checked regardless of net (verified with kicad-cli 10.0.1).
    all_holes = [i for i in items if i.kind == "hole"] if vias else []
    if not (others or vias) or not (segments or vias):
        return report
    margin = max(
        required_mm,
        rules.max_requirement()
        if rules is not None
        else max(
            KICAD_DEFAULT_HOLE_CLEARANCE_MM,
            KICAD_DEFAULT_EDGE_CLEARANCE_MM,
            KICAD_DEFAULT_HOLE_TO_HOLE_MM,
        ),
    )
    index = _Index(others, margin)
    hole_index = _Index(all_holes, margin)

    def _need(kind: str, new: ItemProps, other: ItemProps, layer: str | None) -> float:
        if rules is None:
            return {
                "clearance": required_mm,
                HOLE_CLEARANCE: KICAD_DEFAULT_HOLE_CLEARANCE_MM,
                EDGE_CLEARANCE: KICAD_DEFAULT_EDGE_CLEARANCE_MM,
                HOLE_TO_HOLE: KICAD_DEFAULT_HOLE_TO_HOLE_MM,
            }.get(kind, 0.0)
        return rules.required(kind, new, other, layer)

    def _record(kind: str, new_kind: str, other: ForeignItem, gap: float, need: float) -> bool:
        if kind == "clearance" and gap <= CLEARANCE_EPSILON_MM:
            report.conflicts.append(CopperConflict("short", new_kind, other, gap, need))
            return True
        if kind in _HOLE_KINDS and gap <= CLEARANCE_EPSILON_MM:
            # Copper (or a drill) across a hole: refused even where an
            # ``ignore`` rule switches the check off -- the drill severs it.
            report.conflicts.append(CopperConflict(kind, new_kind, other, gap, need))
            return True
        if need > 0 and gap < need - CLEARANCE_EPSILON_MM:
            report.conflicts.append(CopperConflict(kind, new_kind, other, gap, need))
            return True
        return False

    def _measure(
        kinds: Sequence[str],
        new_kind: str,
        new_props: ItemProps,
        other: ForeignItem,
        gap: float,
        layer: str | None,
    ) -> bool:
        """Record the first of ``kinds`` the gap breaks, if any."""
        other_props = other.props()
        return any(
            _record(kind, new_kind, other, gap, _need(kind, new_props, other_props, layer))
            for kind in kinds
        )

    def _check(
        new: KShape,
        layer: str | None,
        new_kind: str,
        new_props: ItemProps,
        new_hole: KShape | None = None,
    ) -> bool:
        hit = False
        bbox = _shape_bbox(new)
        near = index.near(bbox)
        # Copper first, so a hole inside a pad already in conflict is not
        # reported a second time.
        near.sort(key=lambda o: o.kind == "hole")
        in_conflict: set[str] = set()
        for other in near:
            if other.kind == "hole":
                if other.parent in in_conflict:
                    continue
                gap = min(copper_gap(new, shape) for shape in other.shapes)
                kinds = [HOLE_CLEARANCE, PHYSICAL_HOLE_CLEARANCE]
                if other.slot_edge:
                    kinds.append(EDGE_CLEARANCE)
                # A bare NPTH hole is no copper, but physical_clearance
                # measures to it (verified with kicad-cli 10.0.1).
                kinds.append(PHYSICAL_CLEARANCE)
                if _measure(kinds, new_kind, new_props, other, gap, layer):
                    in_conflict.add(other.label)
                    hit = True
                continue
            if layer is not None and other.layers is not None and layer not in other.layers:
                found = False
            else:
                gap = min(copper_gap(new, shape) for shape in other.shapes)
                found = _measure(
                    ("clearance", PHYSICAL_CLEARANCE), new_kind, new_props, other, gap, layer
                )
            if not found and new_hole is not None:
                # The new via's drill, through every layer, against this copper.
                gap = min(copper_gap(new_hole, shape) for shape in other.shapes)
                found = _measure(
                    (HOLE_CLEARANCE, PHYSICAL_HOLE_CLEARANCE),
                    new_kind,
                    new_props,
                    other,
                    gap,
                    None,
                )
            if found:
                in_conflict.add(other.label)
                hit = True
        if new_hole is not None:
            # The new via's drill against every drilled hole, any net (#6150).
            for other in hole_index.near(_shape_bbox(new_hole)):
                if other.label in in_conflict or other.parent in in_conflict:
                    continue
                gap = min(copper_gap(new_hole, shape) for shape in other.shapes)
                if _measure((HOLE_TO_HOLE,), new_kind, new_props, other, gap, None):
                    hit = True
        return hit

    for seg in segments:
        try:
            shape = KSegment(
                float(seg.x1),
                float(seg.y1),
                float(seg.x2),
                float(seg.y2),
                float(getattr(seg, "width", 0.0) or 0.0),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        layer = _layer_name(getattr(seg, "layer", None))
        report.segments_in_conflict += _check(
            shape, layer, "segment", ItemProps("Track", net_name, layer=layer)
        )

    via_props = ItemProps("Via", net_name, True)
    new_holes: list[tuple[int, KVia]] = []
    via_hits: dict[int, bool] = {}
    for i, via in enumerate(vias):
        try:
            x, y = float(via.x), float(via.y)
            new_via = KVia(x, y, float(getattr(via, "diameter", 0.0) or 0.0))
            drill = float(getattr(via, "drill", 0.0) or 0.0)
        except (AttributeError, TypeError, ValueError):
            continue
        new_hole = KVia(x, y, drill) if drill > 0 else None
        via_hits[i] = _check(new_via, None, "via", via_props, new_hole)
        if new_hole is not None:
            new_holes.append((i, new_hole))

    # The new vias' drills against each other: KiCad checks hole_to_hole
    # within a net too.
    if len(new_holes) > 1:
        need = _need(HOLE_TO_HOLE, via_props, via_props, None)
        for k, (i, hole_i) in enumerate(new_holes):
            for j, hole_j in new_holes[k + 1 :]:
                gap = copper_gap(hole_i, hole_j)
                if gap > need + CLEARANCE_EPSILON_MM:
                    continue
                other = ForeignItem(
                    kind="hole",
                    net=0,
                    net_name=net_name or "",
                    label=f"drill of new via at {describe_point(hole_j.x, hole_j.y, origin)}",
                    shapes=(hole_j,),
                    layers=None,
                    bbox=_shape_bbox(hole_j),
                    owner="via",
                    plated=True,
                )
                if _record(HOLE_TO_HOLE, "via", other, gap, need):
                    via_hits[j] = True
    report.vias_in_conflict += sum(via_hits.values())
    return report


def summarize_conflicts(report: ConflictReport, strategy_name: str) -> str:
    """The refusal message: which nets and holes, which items, how close."""
    parts: list[str] = []
    shorts = report.shorts
    if shorts:
        nets = list(dict.fromkeys(c.other.net_label for c in shorts))
        examples = list(dict.fromkeys(f"{c.other.label} of '{c.other.net_label}'" for c in shorts))
        parts.append(
            f"shorts net(s) {', '.join(repr(n) for n in nets)} already on the board "
            f"(e.g. {'; '.join(examples[:3])})"
        )
    clear = report.clearance
    if clear:
        nets = list(dict.fromkeys(c.other.net_label for c in clear))
        worst = min(clear, key=lambda c: c.gap_mm - c.required_mm)
        if abs(worst.required_mm - report.required_mm) <= CLEARANCE_EPSILON_MM:
            rule = f"below the board's {worst.required_mm:.3f} mm clearance"
        else:
            rule = f"below the {worst.required_mm:.3f} mm clearance that pair requires"
        parts.append(
            f"comes within {worst.gap_mm:.3f} mm of net(s) "
            f"{', '.join(repr(n) for n in nets)} (closest: {worst.other.label} of "
            f"'{worst.other.net_label}'), {rule}"
        )
    physical = report.physical_clearance
    if physical:
        worst = min(physical, key=lambda c: c.gap_mm - c.required_mm)
        examples = list(dict.fromkeys(c.other.label for c in physical))
        parts.append(
            f"comes within {worst.gap_mm:.3f} mm of {'; '.join(examples[:3])}, below the "
            f"{worst.required_mm:.3f} mm physical clearance"
        )
    for conflicts, rule_name in (
        (report.hole_clearance, "hole clearance"),
        (report.physical_hole_clearance, "physical hole clearance"),
        (report.edge_clearance, "board-edge clearance (an NPTH slot is board edge)"),
        (report.hole_to_hole, "hole-to-hole clearance"),
    ):
        if not conflicts:
            continue
        # A new via's drill against copper, or against another hole.
        for group, subject, target in (
            ([c for c in conflicts if c.other.kind != "hole"], "drills a via that ", "copper"),
            (
                [c for c in conflicts if c.other.kind == "hole"],
                "drills a via that " if rule_name == "hole-to-hole clearance" else "",
                "drilled hole(s)",
            ),
        ):
            if not group:
                continue
            worst = min(group, key=lambda c: c.gap_mm - c.required_mm)
            examples = list(dict.fromkeys(c.other.label for c in group))
            how = (
                "crosses"
                if worst.gap_mm <= CLEARANCE_EPSILON_MM
                else f"comes within {worst.gap_mm:.3f} mm of"
            )
            parts.append(
                f"{subject}{how} {target} ({'; '.join(examples[:3])}), below the "
                f"{worst.required_mm:.3f} mm {rule_name}"
            )
    return (
        f"route-auto '{strategy_name}' strategy produced copper that "
        + "; and ".join(parts)
        + f" ({report.segments_in_conflict} segment(s), {report.vias_in_conflict} via(s)); "
        "refusing it rather than writing it (issues #6001, #6107, #6122, #6139, #6150)."
    )


__all__ = [
    "ARC_FLATTEN_ERROR_MM",
    "KICAD_DEFAULT_CLEARANCE_MM",
    "ConflictReport",
    "CopperConflict",
    "ForeignItem",
    "board_clearance_rules",
    "board_copper",
    "board_holes",
    "board_required_clearance",
    "describe_point",
    "find_conflicts",
    "flatten_arc",
    "summarize_conflicts",
]
