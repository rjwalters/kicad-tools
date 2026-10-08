"""Authored per-net clearance minima, judged by the shared clearance kernel (#6243).

A project's ``.kicad_pro`` netclasses can require more clearance for some nets
than the board-wide base the router is configured with -- an isolated
high-voltage net, a sensitive analog input.  KiCad's own DRC then measures the
gap between two foreign nets against ``max(clearance[a], clearance[b])``.
:mod:`kicad_tools.core.project_clearance` resolves those per-net values
(pinned by a 36-case kicad-cli oracle) and the routing loader stores the ones
stricter than the project's ``Default`` class in
:attr:`DesignRules.net_clearance_floors <kicad_tools.router.rules.DesignRules>`.

This module is where those minima meet geometry.  It owns **one** predicate,
used by search, commit and final validation alike:

    two foreign coppers ``a`` and ``b`` are legal only when the kernel's exact
    edge-to-edge gap is at least ``max(floor[a], floor[b])``.

Rule resolution is the pair floor (:func:`pair_floor`); geometry is the
Epic #5509 clearance kernel (:mod:`.clearance_kernel`), reached through
:mod:`.clearance_shapes` so the
copper here is the very copper every other migrated consumer measures.  The C++
backend mirrors this predicate in ``Grid3D::authored_*_clear``
(``cpp/src/grid.cpp``) with the same kernel and the same epsilon, and
``tests/router/test_clearance_kernel_parity.py`` holds the two to identical
verdicts.

Why a separate gate instead of widening the raster
--------------------------------------------------
The grid search's occupancy raster carries many deliberate local reliefs --
pad-exit relaxation, same-component carve-outs, diff-pair partner gaps,
negotiated sharing, HV attach-zone waivers.  Each is legitimate for the fab
scalar it relaxes and wrong for a designer-authored electrical minimum.  So the
minimum is not folded into the raster at all: every search step, every via
placement and every committed segment/via is additionally asked of this gate,
which no relief reaches.  The scalar machinery still enforces the base
clearance exactly as before; this gate only ever *adds* a requirement, and it is
a no-op (``floors`` empty) on every board whose project declares nothing
stricter than ``Default``.

Scope
-----
Copper the gate knows: every pad registered on the grid (including skipped /
placement-excluded pads, whose own authored minimum rides on
:attr:`Pad.authored_clearance <kicad_tools.router.primitives.Pad>` because their
routing net id is neutralised to ``0``), and every committed route segment and
via.  Preserved fills are handled by raising each fill's own clearance to its
source net's minimum where the fills are built (:mod:`.fixed_copper`), which the
fill predicate already honours symmetrically.  Vias are modelled as copper on
every layer (:data:`~.clearance_shapes.ALL_LAYERS`) -- exact for the through
vias the router emits and conservative for a blind/buried barrel.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .clearance_shapes import (
    KShape,
    copper_gap,
    pad_shape,
    segment_shape,
    shapes_clear,
    via_shape,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .primitives import Pad, Route, Segment, Via

__all__ = [
    "AuthoredCopperIndex",
    "AuthoredViolation",
    "authored_violations",
    "pad_authored_floor",
    "pair_floor",
]

#: Spatial bucket edge in mm.  Buckets only *select* candidates; every verdict
#: is the kernel's.  1 mm matches ``Grid3D``'s fixed-fill bins.
_BUCKET_MM = 1.0


def pair_floor(
    floors: Mapping[int, float],
    net_a: int,
    net_b: int,
    own_a: float = 0.0,
    own_b: float = 0.0,
) -> float:
    """The authored minimum two coppers must keep apart (0.0 = none).

    Same-net copper is never a clearance obstacle, so it needs nothing.  Each
    side contributes the larger of its net's floor and its item-level floor
    (``own_*``, e.g. a neutralised pad's :attr:`Pad.authored_clearance`).
    """
    if net_a == net_b:
        return 0.0
    return max(floors.get(net_a, 0.0), floors.get(net_b, 0.0), own_a, own_b)


def pad_authored_floor(pad: Pad, floors: Mapping[int, float]) -> float:
    """A pad's own authored minimum: its net's floor or its item-level floor."""
    return max(floors.get(pad.net, 0.0), float(getattr(pad, "authored_clearance", 0.0) or 0.0))


@dataclass(frozen=True)
class AuthoredViolation:
    """One authored-minimum shortfall between two foreign coppers."""

    x: float
    y: float
    net: int
    other_net: int
    required: float
    actual: float
    kind: str  # "pad" | "segment" | "via"


@dataclass(frozen=True)
class _Item:
    shape: KShape
    net: int
    floor: float
    kind: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]


def _seg_bbox(seg: Segment) -> tuple[float, float, float, float]:
    h = seg.width / 2.0
    return (
        min(seg.x1, seg.x2) - h,
        min(seg.y1, seg.y2) - h,
        max(seg.x1, seg.x2) + h,
        max(seg.y1, seg.y2) + h,
    )


def _via_bbox(via: Via) -> tuple[float, float, float, float]:
    r = via.diameter / 2.0
    return (via.x - r, via.y - r, via.x + r, via.y + r)


def _pad_bbox(pad: Pad) -> tuple[float, float, float, float]:
    # Encloses every rotation of the pad rectangle (and the circle radius).
    r = math.hypot(pad.width, pad.height) / 2.0
    return (pad.x - r, pad.y - r, pad.x + r, pad.y + r)


class AuthoredCopperIndex:
    """Bucketed foreign copper the authored-minimum predicate is asked against.

    Built from a pad census and a route list once, then queried per candidate.
    Two bucket maps are kept: every item, and only items that themselves carry
    a floor.  A candidate whose own net has no floor can only conflict with
    floored copper, so it walks the (typically tiny) second map -- which is
    what keeps the gate cheap on a board where one isolated net is strict.
    """

    def __init__(
        self,
        floors: Mapping[int, float],
        pads: Iterable[Pad] = (),
        routes: Iterable[Route] = (),
    ) -> None:
        self.floors: dict[int, float] = dict(floors)
        self.max_floor = max(self.floors.values(), default=0.0)
        self._all: dict[tuple[int, int], list[_Item]] = {}
        self._floored: dict[tuple[int, int], list[_Item]] = {}
        self._max_item_floor = 0.0
        for pad in pads:
            self._add(
                _Item(
                    pad_shape(pad),
                    pad.net,
                    pad_authored_floor(pad, self.floors),
                    "pad",
                    pad.x,
                    pad.y,
                    _pad_bbox(pad),
                )
            )
        for route in routes:
            floor = max(
                self.floors.get(route.net, 0.0),
                float(getattr(route, "authored_clearance", 0.0) or 0.0),
            )
            for seg in route.segments:
                self._add(
                    _Item(
                        segment_shape(seg),
                        route.net,
                        floor,
                        "segment",
                        (seg.x1 + seg.x2) / 2.0,
                        (seg.y1 + seg.y2) / 2.0,
                        _seg_bbox(seg),
                    )
                )
            for via in route.vias:
                self._add(
                    _Item(via_shape(via), route.net, floor, "via", via.x, via.y, _via_bbox(via))
                )

    @property
    def active(self) -> bool:
        """True when any copper carries an authored minimum at all."""
        return self.max_floor > 0.0 or self._max_item_floor > 0.0

    def _add(self, item: _Item) -> None:
        if item.floor > self._max_item_floor:
            self._max_item_floor = item.floor
        for key in _bucket_keys(item.bbox, 0.0):
            self._all.setdefault(key, []).append(item)
            if item.floor > 0.0:
                self._floored.setdefault(key, []).append(item)

    def _candidates(
        self, bbox: tuple[float, float, float, float], own_floor: float
    ) -> Iterable[_Item]:
        if own_floor > 0.0:
            buckets, reach = self._all, max(own_floor, self._max_item_floor)
        else:
            buckets, reach = self._floored, self._max_item_floor
        if not buckets or reach <= 0.0:
            return ()
        seen: set[int] = set()
        out: list[_Item] = []
        for key in _bucket_keys(bbox, reach):
            for item in buckets.get(key, ()):
                if id(item) in seen:
                    continue
                seen.add(id(item))
                out.append(item)
        return out

    def first_violation(
        self, shape: KShape, bbox: tuple[float, float, float, float], net: int, own_floor=None
    ) -> AuthoredViolation | None:
        """The first foreign copper ``shape`` (owned by ``net``) is too close to.

        ``own_floor`` defaults to the net's floor; a caller holding an
        item-level floor (a neutral pad) passes it explicitly.
        """
        own = self.floors.get(net, 0.0) if own_floor is None else own_floor
        for item in self._candidates(bbox, own):
            if item.net == net:
                continue
            required = max(own, item.floor)
            if required <= 0.0:
                continue
            if not shapes_clear(shape, item.shape, required):
                return AuthoredViolation(
                    item.x,
                    item.y,
                    net,
                    item.net,
                    required,
                    copper_gap(shape, item.shape),
                    item.kind,
                )
        return None

    def candidate_mask(
        self,
        net: int,
        own_radius: float,
        *,
        origin: tuple[float, float],
        resolution: float,
        shape: tuple[int, int, int],
        layer_values: list[int | None],
        all_layers: bool = False,
    ) -> Any:
        """Grid cells where ``net``'s copper could come within an authored minimum.

        A search-speed broad phase, never a verdict: cell ``(L, y, x)`` is
        ``False`` only when copper of radius ``own_radius`` centred anywhere
        within one grid step of that cell centre provably clears every foreign
        item's authored minimum (each item's bounding box grown by
        ``required + own_radius + resolution``).  A ``True`` cell still goes to
        the exact kernel predicate.  ``all_layers`` folds every layer into
        each plane (a through-via probe).

        Args:
            net: The querying net.
            own_radius: The probe copper's half-width (trace) or radius (via).
            origin: Grid origin ``(x, y)`` in mm.
            resolution: Grid pitch in mm.
            shape: ``(layers, rows, cols)``.
            layer_values: Per grid layer, the kernel layer value its copper
                carries (``Layer.value``), or ``None`` when unmapped.
            all_layers: Treat the probe as present on every layer.

        Returns:
            A boolean ``numpy`` array of ``shape``.
        """
        import numpy as np

        from .clearance_shapes import ALL_LAYERS

        layers, rows, cols = shape
        mask = np.zeros(shape, dtype=bool)
        own = self.floors.get(net, 0.0)
        buckets = self._all if own > 0.0 else self._floored
        seen: set[int] = set()
        ox, oy = origin
        for bucket in buckets.values():
            for item in bucket:
                if id(item) in seen or item.net == net:
                    continue
                seen.add(id(item))
                required = max(own, item.floor)
                if required <= 0.0:
                    continue
                grow = required + own_radius + resolution
                x0 = max(0, math.floor((item.bbox[0] - grow - ox) / resolution))
                y0 = max(0, math.floor((item.bbox[1] - grow - oy) / resolution))
                x1 = min(cols - 1, math.ceil((item.bbox[2] + grow - ox) / resolution))
                y1 = min(rows - 1, math.ceil((item.bbox[3] + grow - oy) / resolution))
                if x0 > x1 or y0 > y1:
                    continue
                item_layer = getattr(item.shape, "layer", ALL_LAYERS)
                for idx in range(layers):
                    if (
                        not all_layers
                        and item_layer != ALL_LAYERS
                        and layer_values[idx] is not None
                        and layer_values[idx] != item_layer
                    ):
                        continue
                    mask[idx, y0 : y1 + 1, x0 : x1 + 1] = True
        return mask

    def segment_violation(self, seg: Segment, net: int | None = None) -> AuthoredViolation | None:
        """Authored-minimum check for a trace segment (``net`` defaults to ``seg.net``)."""
        return self.first_violation(
            segment_shape(seg), _seg_bbox(seg), seg.net if net is None else net
        )

    def via_violation(self, via: Via, net: int | None = None) -> AuthoredViolation | None:
        """Authored-minimum check for a via barrel on every layer."""
        return self.first_violation(via_shape(via), _via_bbox(via), via.net if net is None else net)

    def segment_clear(self, seg: Segment, net: int | None = None) -> bool:
        return self.segment_violation(seg, net) is None

    def via_clear(self, via: Via, net: int | None = None) -> bool:
        return self.via_violation(via, net) is None


def _bucket_keys(bbox: tuple[float, float, float, float], reach: float) -> list[tuple[int, int]]:
    x0 = math.floor((bbox[0] - reach) / _BUCKET_MM)
    y0 = math.floor((bbox[1] - reach) / _BUCKET_MM)
    x1 = math.floor((bbox[2] + reach) / _BUCKET_MM)
    y1 = math.floor((bbox[3] + reach) / _BUCKET_MM)
    return [(bx, by) for bx in range(x0, x1 + 1) for by in range(y0, y1 + 1)]


def authored_violations(
    floors: Mapping[int, float],
    pads: Iterable[Pad],
    routes: Iterable[Route],
    *,
    first_per_net: bool = False,
) -> list[AuthoredViolation]:
    """Every committed segment/via that breaks an authored minimum.

    The final-validation census: the same predicate the search gate and the
    commit validators ask, run over finished copper.  Each offending pair is
    reported once per offending segment/via (from the side being scanned).
    """
    routes = list(routes)
    index = AuthoredCopperIndex(floors, pads, routes)
    if not index.active:
        return []
    found: list[AuthoredViolation] = []
    flagged: set[int] = set()
    for route in routes:
        if first_per_net and route.net in flagged:
            continue
        for seg in route.segments:
            hit = index.segment_violation(seg, route.net)
            if hit is not None:
                found.append(hit)
                flagged.add(route.net)
                if first_per_net:
                    break
        if first_per_net and route.net in flagged:
            continue
        for via in route.vias:
            hit = index.via_violation(via, route.net)
            if hit is not None:
                found.append(hit)
                flagged.add(route.net)
                if first_per_net:
                    break
    return found


_EMPTY: list[Any] = []


def grid_authored_index(grid: Any) -> AuthoredCopperIndex | None:
    """The grid's cached index of its own pads and committed routes, or ``None``.

    ``None`` -- the overwhelmingly common case -- means the grid's rules carry
    no authored minima and no registered pad or marked route carries an
    item-level one (``RoutingGrid._authored_item_floors``), so
    every caller short-circuits.  The cache is keyed on the copper the index
    was built from (route/pad list identity, length and last element, and the
    floors mapping), so any mark/unmark or pad registration rebuilds it before
    the next query.
    """
    rules = getattr(grid, "rules", None)
    floors = getattr(rules, "net_clearance_floors", None) or {}
    # NB: never ``or []`` here -- a fresh empty list per call would change the
    # cache key below on every query and rebuild the index each time.
    pads = getattr(grid, "_pads", None)
    if pads is None:
        pads = _EMPTY
    if not isinstance(floors, dict):
        floors = {}
    if not floors and getattr(grid, "_authored_item_floors", False) is not True:
        return None
    routes = getattr(grid, "routes", None)
    if routes is None:
        routes = _EMPTY
    key = (
        id(floors),
        len(floors),
        id(routes),
        len(routes),
        id(routes[-1]) if routes else 0,
        id(pads),
        len(pads),
        id(pads[-1]) if pads else 0,
    )
    cached = getattr(grid, "_authored_index_cache", None)
    if cached is not None and cached[0] == key:
        hit: AuthoredCopperIndex | None = cached[1]
        return hit
    index = AuthoredCopperIndex(floors, pads, routes)
    result = index if index.active else None
    grid._authored_index_cache = (key, result)
    return result
