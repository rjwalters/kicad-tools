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
:func:`board_required_clearance` returns the copper clearance KiCad measures
the board against: the strictest of the ``.kicad_dru`` rule, the ``.kicad_pro``
board minimum and the ``.kicad_pro`` netclass clearance
(:func:`~kicad_tools.router.clearance_resolver.read_declared_clearance_rules`).
A board that declares none gets KiCad's built-in ``Default`` netclass
clearance, 0.2 mm, because that is what ``kicad-cli pcb drc`` then applies.
The netclass is the project's ``Default`` class (or, without one, the
strictest class); a per-net class assignment is not resolved.

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

#: KiCad's built-in ``Default`` netclass clearance, in mm.  ``kicad-cli pcb
#: drc`` applies it to a board whose project declares no clearance at all.
KICAD_DEFAULT_CLEARANCE_MM = 0.2

#: Sagitta bound when an arc is flattened for the gate or the routing grid.
#: The flattened copper is widened by twice this, so it always covers the arc.
ARC_FLATTEN_ERROR_MM = 0.001

_BUCKET_MM = 2.0


@dataclass(frozen=True)
class ForeignItem:
    """One piece of copper already on the board.

    Attributes:
        kind: ``"track"``, ``"arc"``, ``"via"`` or ``"pad"``.
        net: Net number (0 for unassigned copper).
        net_name: Net name, ``""`` when the board gives none.
        label: Human-readable identity, e.g. ``pad R5.1`` or
            ``track (102.000, 108.000)-(128.000, 108.000) on F.Cu``.
        shapes: Kernel copper.  An arc is several flattened segments.
        layers: Copper layer names the item is on; ``None`` means every
            copper layer (vias, through-hole pads, ``*.Cu`` pads).
        bbox: ``(min_x, min_y, max_x, max_y)`` of the copper itself.
    """

    kind: str
    net: int
    net_name: str
    label: str
    shapes: tuple[KShape, ...]
    layers: frozenset[str] | None
    bbox: tuple[float, float, float, float]

    @property
    def net_label(self) -> str:
        return self.net_name or f"Net_{self.net}"


@dataclass(frozen=True)
class CopperConflict:
    """New copper that shorts, or is too close to, another net's copper."""

    kind: str  # "short" or "clearance"
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
                    drill = float(getattr(pad, "drill", 0.0) or 0.0)
                    if str(getattr(pad, "type", "")) == "np_thru_hole" and max(w, h) <= drill:
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
                    )
                )
    return items


# ---------------------------------------------------------------------------
# The clearance requirement
# ---------------------------------------------------------------------------


def board_required_clearance(pcb_path: str | Path | None, fallback_mm: float) -> float:
    """The copper clearance KiCad's DRC measures this board against, in mm.

    The strictest of the board's ``.kicad_dru`` / ``.kicad_pro`` declarations;
    KiCad's own default (0.2 mm) when the board has a file but declares none;
    ``fallback_mm`` (the router's own clearance) when there is no file.
    """
    if pcb_path is None:
        return fallback_mm
    from .clearance_resolver import read_declared_clearance_rules

    declared = read_declared_clearance_rules(pcb_path)
    requirement = declared.project_requirement()
    if requirement is None:
        return KICAD_DEFAULT_CLEARANCE_MM
    return float(requirement[0])


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


def find_conflicts(
    items: Sequence[ForeignItem],
    segments: Sequence[Any],
    vias: Sequence[Any],
    *,
    own_ids: set[int],
    own_names: set[str],
    required_mm: float,
) -> ConflictReport:
    """New router copper that shorts, or is closer than ``required_mm`` to,
    another net's copper.

    ``segments`` / ``vias`` are router primitives (``x1``/``y1``/``x2``/``y2``
    + ``width`` + ``layer``; ``x``/``y`` + ``diameter``).  A via is treated as
    a through via (on every layer), which errs toward refusing.  Copper of the
    routed net (``own_ids`` / ``own_names``) and net-0 tracks, vias and arcs are
    exempt; net-0 pads are not.
    """
    report = ConflictReport(required_mm=required_mm)

    def _foreign(item: ForeignItem) -> bool:
        if item.net_name and item.net_name in own_names:
            return False
        if item.kind == "pad":
            return item.net == 0 or item.net not in own_ids
        return item.net > 0 and item.net not in own_ids

    others = [i for i in items if _foreign(i)]
    if not others or not (segments or vias):
        return report
    index = _Index(others, required_mm)

    def _check(new: KShape, layer: str | None, kind: str) -> bool:
        hit = False
        for other in index.near(_shape_bbox(new)):
            if layer is not None and other.layers is not None and layer not in other.layers:
                continue
            gap = min(copper_gap(new, shape) for shape in other.shapes)
            if gap <= CLEARANCE_EPSILON_MM:
                report.conflicts.append(CopperConflict("short", kind, other, gap, required_mm))
                hit = True
            elif gap < required_mm - CLEARANCE_EPSILON_MM:
                report.conflicts.append(CopperConflict("clearance", kind, other, gap, required_mm))
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
        report.segments_in_conflict += _check(
            shape, _layer_name(getattr(seg, "layer", None)), "segment"
        )
    for via in vias:
        try:
            new_via = KVia(float(via.x), float(via.y), float(getattr(via, "diameter", 0.0) or 0.0))
        except (AttributeError, TypeError, ValueError):
            continue
        report.vias_in_conflict += _check(new_via, None, "via")
    return report


def summarize_conflicts(report: ConflictReport, strategy_name: str) -> str:
    """The refusal message: which nets, which items, how close."""
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
        worst = min(clear, key=lambda c: c.gap_mm)
        parts.append(
            f"comes within {worst.gap_mm:.3f} mm of net(s) "
            f"{', '.join(repr(n) for n in nets)} (closest: {worst.other.label} of "
            f"'{worst.other.net_label}'), below the board's "
            f"{report.required_mm:.3f} mm clearance"
        )
    return (
        f"route-auto '{strategy_name}' strategy produced copper that "
        + "; and ".join(parts)
        + f" ({report.segments_in_conflict} segment(s), {report.vias_in_conflict} via(s)); "
        "refusing it rather than writing it (issues #6001, #6107)."
    )


__all__ = [
    "ARC_FLATTEN_ERROR_MM",
    "KICAD_DEFAULT_CLEARANCE_MM",
    "ConflictReport",
    "CopperConflict",
    "ForeignItem",
    "board_copper",
    "board_required_clearance",
    "describe_point",
    "find_conflicts",
    "flatten_arc",
    "summarize_conflicts",
]
