"""Per-pad **access sets** -- the legal ways copper can still leave a pad.

Epic #5508 / Phase 1a.  This module is **pure geometry and read-only**: it
answers "given the copper committed so far, can this pad still be reached?"
without routing anything, without mutating the grid, and without introducing a
router knob.  Phase 1b consumes it to build an offline witness ("which commit
stranded which pad?") and Phase 2 turns it into a commit-time invariant.

Definitions
-----------

The *access set* of a routing terminal is

* every **exit stub** -- a short own-layer trace leaving the terminal's metal in
  one of the eight A* directions, long enough to clear the terminal's own
  clearance halo (``trace_width + 2 * trace_clearance``, rounded up to whole
  grid cells); plus
* every **via site** reachable from the terminal -- the terminal centre itself
  when the fab tier supports via-in-pad, and the far end of each *legal* exit
  stub.

A terminal whose access set :meth:`AccessSet.is_empty` is *stranded*: no first
move exists, so no route to it can exist either, whatever the search does
afterwards.  When (and only when) the set is empty the result carries
:attr:`AccessSet.closing_copper` -- the copper items that rejected every
candidate -- so the witness can name the nets responsible.

Two design decisions are load-bearing (both traced in #5516):

1. **Legality is decided by world-coordinate geometric predicates, never by the
   raster.**  Every accept/reject goes through the *existing* validators
   (:meth:`RoutingGrid.validate_segment_clearance`,
   :meth:`RoutingGrid.validate_via_clearance`,
   :meth:`RoutingGrid.validate_via_to_via_clearance`,
   :func:`via_clearance.point_clear_of_copper`,
   :func:`via_clearance.drill_hole_to_hole_clear`) behind the
   :class:`AccessLegality` adapter, with the rule values (``trace_clearance``,
   ``via_clearance``, ``min_hole_to_hole``) passed through **unchanged** -- this
   module performs no clearance arithmetic of its own and relaxes nothing.  The
   grid's blocked/obstacle raster is read only *after* a rejection, to label
   :attr:`ClosingCopper.marking`.  That split is what makes the #5410 ``DQS_N``
   case come out right: a candidate whose coarse raster halo says "blocked" but
   whose true copper gap clears the rule is **legal**, and the access set says so.

2. **Same-net copper is invisible to the predicates** (they all skip
   ``net == exclude_net``), which is correct for ordinary nets and wrong for a
   Kelvin sense/force pair whose sibling branch is a hard obstacle for exactly
   one edge search (:func:`kelvin_obstacles.isolate_kelvin_branch`).  Callers
   that know the topology pass those siblings as ``hard_same_net`` and the
   adapter treats them as foreign.

The module holds no reference to mutable grid state in its results: every
dataclass is frozen and carries world millimetres only.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from .layers import Layer
from .mfr_limits import MfrLimits, get_mfr_limits
from .pad_geometry import pad_local_point, pad_point_distance, pad_segment_distance
from .primitives import Pad, Route, Segment, Via, pad_half_extents
from .rules import DesignRules
from .via_clearance import (
    ForeignPadTuple,
    TrackSegmentLike,
    drill_hole_to_hole_clear,
    point_clear_of_copper,
)

__all__ = [
    "AccessLegality",
    "AccessSet",
    "ClosingCopper",
    "DefaultAccessLegality",
    "ExitStub",
    "ViaSite",
    "affected_pads",
    "compute_access_set",
    "direction_name",
    "route_envelope",
]

#: Floating-point slack shared by every comparison in this module.  Matches the
#: tolerance the grid validators already use for borderline geometry.
EPS = 1e-9

#: The eight A* moves, in a fixed order so results are deterministic.  ``+y`` is
#: south in board coordinates (KiCad's Y axis grows downward).
DIRECTIONS: tuple[tuple[int, int], ...] = (
    (0, -1),
    (1, -1),
    (1, 0),
    (1, 1),
    (0, 1),
    (-1, 1),
    (-1, 0),
    (-1, -1),
)

_DIRECTION_NAMES: dict[tuple[int, int], str] = {
    (0, -1): "N",
    (1, -1): "NE",
    (1, 0): "E",
    (1, 1): "SE",
    (0, 1): "S",
    (-1, 1): "SW",
    (-1, 0): "W",
    (-1, -1): "NW",
}

ClosingKind = Literal[
    "foreign_pad",
    "route_segment",
    "route_via",
    "fixed_fill",
    "keepout",
    "reserved_hard",
    "kelvin_isolated",
    "board_edge",
]


def direction_name(direction: tuple[int, int]) -> str:
    """Compass label for one of :data:`DIRECTIONS` (``"N"``, ``"NE"``, ...)."""
    return _DIRECTION_NAMES.get(direction, f"{direction}")


# ---------------------------------------------------------------------------
# Result shapes (the contract Phase 1b and Phase 2 consume)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExitStub:
    """A legal own-layer trace leaving the terminal's metal in one direction.

    ``(x0, y0)`` is on the terminal's metal boundary along ``direction``;
    ``(x1, y1)`` is the far end, which is also the via site candidate for this
    stub.  ``width`` is the caller's trace width (``rules.trace_width`` unless a
    net class overrides it), never a re-derived value.
    """

    layer: Layer
    x0: float
    y0: float
    x1: float
    y1: float
    width: float
    direction: tuple[int, int]

    @property
    def name(self) -> str:
        """Compass label of this stub's direction."""
        return direction_name(self.direction)

    def to_segment(self, net: int, net_name: str = "") -> Segment:
        """Materialise this stub as a router :class:`Segment`."""
        return Segment(
            x1=self.x0,
            y1=self.y0,
            x2=self.x1,
            y2=self.y1,
            width=self.width,
            layer=self.layer,
            net=net,
            net_name=net_name,
        )


@dataclass(frozen=True)
class ViaSite:
    """A legal layer transition reachable from the terminal.

    Candidates are exactly two kinds: the terminal centre (``in_pad=True``,
    enumerated only when the configured fab tier reports
    ``via_in_pad_supported`` and the terminal is not through-hole), and the far
    endpoint of each legal :class:`ExitStub` (``from_stub`` indexes
    :attr:`AccessSet.stubs`).  A single-layer stack therefore yields no via
    sites at all, and a terminal whose every stub is closed yields none either
    unless via-in-pad is available.
    """

    x: float
    y: float
    drill: float
    diameter: float
    layers: tuple[Layer, Layer]
    in_pad: bool
    from_stub: int | None

    def to_via(self, net: int, net_name: str = "") -> Via:
        """Materialise this site as a router :class:`Via`."""
        return Via(
            x=self.x,
            y=self.y,
            drill=self.drill,
            diameter=self.diameter,
            layers=self.layers,
            net=net,
            net_name=net_name,
            in_pad=self.in_pad,
        )


@dataclass(frozen=True)
class ClosingCopper:
    """One copper item that rejected an access candidate.

    ``marking`` is read from the grid raster at the item's closest point to the
    rejected candidate and is **descriptive only** -- it never participates in
    the legality decision.  ``shareable`` records whether negotiation could in
    principle price this item (usage-shared route copper) rather than having to
    route around it; the epic's central claim is that closing copper usually is
    not shareable.
    """

    kind: ClosingKind
    net: int
    net_name: str
    ref: str
    pin: str
    layer: Layer
    marking: frozenset[str]
    shareable: bool
    bbox: tuple[float, float, float, float]

    @property
    def sort_key(self) -> tuple[str, int, str, str, int]:
        """Deterministic ordering key (``kind, net, ref, pin, layer``)."""
        return (self.kind, self.net, self.ref, self.pin, self.layer.value)


@dataclass(frozen=True)
class AccessSet:
    """Everything still legal as a *first move* out of one routing terminal."""

    pad_key: tuple[str, str]
    net: int
    origin: tuple[float, float]
    origin_layer: Layer
    origin_is_escape_terminal: bool
    stubs: tuple[ExitStub, ...]
    via_sites: tuple[ViaSite, ...]
    closing_copper: tuple[ClosingCopper, ...]
    bbox: tuple[float, float, float, float]
    fingerprint: tuple[float, float, float, float, str | None, int]

    def is_empty(self) -> bool:
        """True when no legal first move out of the terminal remains."""
        return not self.stubs and not self.via_sites

    def stub_for(self, direction: tuple[int, int]) -> ExitStub | None:
        """Return the legal stub in ``direction``, or None when it is closed."""
        for stub in self.stubs:
            if stub.direction == direction:
                return stub
        return None

    def closing_refs(self) -> tuple[str, ...]:
        """Sorted ``ref.pin`` (or ``ref``) labels of the closing copper items."""
        return tuple(
            sorted(
                {f"{item.ref}.{item.pin}" if item.pin else item.ref for item in self.closing_copper}
            )
        )


# ---------------------------------------------------------------------------
# Legality adapter
# ---------------------------------------------------------------------------


@runtime_checkable
class AccessLegality(Protocol):
    """Decides whether one access candidate is geometrically legal.

    Phase 2 of the epic (#5509's clearance kernel) swaps an implementation in
    here without touching :func:`compute_access_set`.  Both methods return
    ``(is_legal, violation_location)``; the location, when present, is a world
    point near the worst violation and is used only for labelling.
    """

    def stub_legal(
        self, seg: Segment, net: int
    ) -> tuple[bool, tuple[float, float] | None]:  # pragma: no cover - protocol
        ...

    def via_legal(
        self, via: Via, net: int
    ) -> tuple[bool, tuple[float, float] | None]:  # pragma: no cover - protocol
        ...


@dataclass
class _TrackAdapter:
    """``TrackSegmentLike`` view over a router :class:`Segment`."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float


class DefaultAccessLegality:
    """Delegates every decision to the router's existing clearance predicates.

    Nothing here derives or relaxes a clearance: ``rules.trace_clearance``,
    ``rules.via_clearance`` and ``rules.min_hole_to_hole`` are forwarded to the
    validators exactly as configured.

    ``hard_same_net`` names same-net copper that must nonetheless be treated as
    a hard obstacle -- the Kelvin sibling-branch case, which every geometric
    predicate would otherwise skip because it shares the terminal's net.
    """

    def __init__(
        self,
        grid: object,
        rules: DesignRules,
        *,
        hard_same_net: Sequence[Route | Pad] = (),
    ) -> None:
        self.grid = grid
        self.rules = rules
        self.hard_same_net: tuple[Route | Pad, ...] = tuple(hard_same_net)
        self._hard_pads: tuple[Pad, ...] = tuple(
            item for item in self.hard_same_net if isinstance(item, Pad)
        )
        self._hard_routes: tuple[Route, ...] = tuple(
            item for item in self.hard_same_net if isinstance(item, Route)
        )

    # -- stubs ------------------------------------------------------------

    def stub_legal(self, seg: Segment, net: int) -> tuple[bool, tuple[float, float] | None]:
        """True when ``seg`` clears every foreign obstacle at ``trace_clearance``."""
        is_valid, _actual, loc = self.grid.validate_segment_clearance(  # type: ignore[attr-defined]
            seg,
            exclude_net=net,
            min_clearance=self.rules.trace_clearance,
        )
        if not is_valid:
            return False, loc
        hard_loc = self._hard_same_net_blocks_segment(seg)
        if hard_loc is not None:
            return False, hard_loc
        return True, None

    def _hard_same_net_blocks_segment(self, seg: Segment) -> tuple[float, float] | None:
        """Explicit ``hard_same_net`` check at the unchanged trace clearance."""
        required = self.rules.trace_clearance + seg.width / 2
        for pad in self._hard_pads:
            if not pad.through_hole and pad.layer != seg.layer:
                continue
            if pad_segment_distance(pad, seg.x1, seg.y1, seg.x2, seg.y2) < required - EPS:
                return (pad.x, pad.y)
        for route in self._hard_routes:
            for other in route.segments:
                if other.layer != seg.layer:
                    continue
                gap = _segment_segment_distance(seg, other) - (seg.width + other.width) / 2
                if gap < self.rules.trace_clearance - EPS:
                    return (other.x1, other.y1)
            for via in route.vias:
                dist = _point_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)
                if dist - via.diameter / 2 - seg.width / 2 < self.rules.trace_clearance - EPS:
                    return (via.x, via.y)
        return None

    # -- vias -------------------------------------------------------------

    def via_legal(self, via: Via, net: int) -> tuple[bool, tuple[float, float] | None]:
        """True when ``via`` clears foreign copper, foreign vias and drills."""
        grid = self.grid
        is_valid, _actual, loc = grid.validate_via_clearance(  # type: ignore[attr-defined]
            via,
            exclude_net=net,
            min_clearance=self.rules.via_clearance,
        )
        if not is_valid:
            return False, loc
        is_valid, _actual, loc = grid.validate_via_to_via_clearance(  # type: ignore[attr-defined]
            via,
            exclude_net=net,
            min_clearance=self.rules.via_clearance,
        )
        if not is_valid:
            return False, loc

        # ``validate_via_clearance`` covers fixed fills + foreign SEGMENTS only,
        # and ``validate_via_to_via_clearance`` covers foreign vias -- neither
        # sees foreign PADS.  ``point_clear_of_copper`` is the canonical
        # world-coordinate predicate that does (#5516 citation correction).
        if not point_clear_of_copper(
            via.x,
            via.y,
            via.diameter,
            self.rules.via_clearance,
            other_net_tracks=self._foreign_tracks(net),
            other_net_vias=self._foreign_vias(net),
            other_net_pads=self._foreign_pads(net),
        ):
            return False, (via.x, via.y)

        if not drill_hole_to_hole_clear(
            via.x,
            via.y,
            via.drill,
            self.drill_registry(exclude_point=(via.x, via.y)),
            self.rules.min_hole_to_hole,
        ):
            return False, (via.x, via.y)
        return True, None

    # -- copper inventories -----------------------------------------------

    def _pads(self) -> Sequence[Pad]:
        return getattr(self.grid, "_pads", ())

    def _routes(self) -> Sequence[Route]:
        return getattr(self.grid, "routes", ())

    def _foreign_pads(self, net: int) -> list[ForeignPadTuple]:
        """Foreign pads as rect-aware 5-tuples; a through via spans every layer."""
        pads: list[ForeignPadTuple] = []
        for pad, _is_hard in _iter_obstacle_pads(self.grid, net, self._hard_pads):
            half_w, half_h = pad_half_extents(pad)
            pads.append((pad.x, pad.y, half_w * 2, half_h * 2, pad.net))
        return pads

    def _foreign_tracks(self, net: int) -> list[TrackSegmentLike]:
        tracks: list[TrackSegmentLike] = []
        for route, _is_hard in _iter_obstacle_routes(self.grid, net, self._hard_routes):
            for seg in route.segments:
                tracks.append(_TrackAdapter(seg.x1, seg.y1, seg.x2, seg.y2, seg.width))
        return tracks

    def _foreign_vias(self, net: int) -> list[tuple[float, float, float, int]]:
        vias: list[tuple[float, float, float, int]] = []
        for route, _is_hard in _iter_obstacle_routes(self.grid, net, self._hard_routes):
            for via in route.vias:
                vias.append((via.x, via.y, via.diameter, via.net))
        return vias

    def drill_registry(
        self, *, exclude_point: tuple[float, float] | None = None
    ) -> list[tuple[float, float, float]]:
        """Board-wide drill registry, mirroring ``core.py``'s canonical builder.

        A drill floor is mechanical, so it applies regardless of net: every
        through-hole pad drill plus every committed via drill participates.
        ``exclude_point`` drops an exact-coordinate self-match so a candidate
        never conflicts with its own hole.
        """
        drills: list[tuple[float, float, float]] = []
        for pad in self._pads():
            if not getattr(pad, "through_hole", False):
                continue
            drill = float(getattr(pad, "drill", 0.0))
            if drill > 0.0:
                drills.append((float(pad.x), float(pad.y), drill))
        for route in self._routes():
            for via in route.vias:
                drills.append((float(via.x), float(via.y), float(via.drill)))
        if exclude_point is not None:
            ex, ey = exclude_point
            drills = [d for d in drills if abs(d[0] - ex) > EPS or abs(d[1] - ey) > EPS]
        return drills


# ---------------------------------------------------------------------------
# Obstacle inventories
# ---------------------------------------------------------------------------


def _iter_obstacle_pads(grid: object, net: int, hard_pads: Sequence[Pad]) -> list[tuple[Pad, bool]]:
    """Pads that act as obstacles for ``net``: every foreign pad, plus the
    caller-declared hard same-net pads (Kelvin siblings).

    Identity -- not ``==`` -- decides hard membership: :class:`Pad` is a plain
    dataclass, so two geometrically identical pads compare equal and a value
    test would mislabel an ordinary foreign pad as Kelvin-isolated.
    """
    hard_ids = {id(pad) for pad in hard_pads}
    out: list[tuple[Pad, bool]] = []
    seen: set[int] = set()
    for pad in list(getattr(grid, "_pads", ())) + list(hard_pads):
        if id(pad) in seen:
            continue
        seen.add(id(pad))
        is_hard = id(pad) in hard_ids
        if pad.net == net and not is_hard:
            continue
        out.append((pad, is_hard))
    return out


def _iter_obstacle_routes(
    grid: object, net: int, hard_routes: Sequence[Route]
) -> list[tuple[Route, bool]]:
    """Routes that act as obstacles for ``net`` (see :func:`_iter_obstacle_pads`)."""
    hard_ids = {id(route) for route in hard_routes}
    out: list[tuple[Route, bool]] = []
    seen: set[int] = set()
    for route in list(getattr(grid, "routes", ())) + list(hard_routes):
        if id(route) in seen:
            continue
        seen.add(id(route))
        is_hard = id(route) in hard_ids
        if route.net == net and not is_hard:
            continue
        out.append((route, is_hard))
    return out


# ---------------------------------------------------------------------------
# Small geometry helpers (labelling / envelopes only -- never legality)
# ---------------------------------------------------------------------------


def _point_segment_distance(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> float:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return math.hypot(px - x1, py - y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _closest_point_on_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float
) -> tuple[float, float]:
    dx, dy = x2 - x1, y2 - y1
    if dx == 0.0 and dy == 0.0:
        return (x1, y1)
    t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return (x1 + t * dx, y1 + t * dy)


def _contact_on_segment(
    candidate: Segment, x1: float, y1: float, x2: float, y2: float
) -> tuple[float, float]:
    """Point on the obstacle segment ``(x1,y1)-(x2,y2)`` nearest ``candidate``.

    Approximated by projecting whichever candidate endpoint is closer -- enough
    to land inside the obstacle's own copper so the raster read in
    :func:`_marking_at` describes the obstacle, not the candidate.
    """
    d_start = _point_segment_distance(candidate.x1, candidate.y1, x1, y1, x2, y2)
    d_end = _point_segment_distance(candidate.x2, candidate.y2, x1, y1, x2, y2)
    if d_end < d_start:
        return _closest_point_on_segment(candidate.x2, candidate.y2, x1, y1, x2, y2)
    return _closest_point_on_segment(candidate.x1, candidate.y1, x1, y1, x2, y2)


def _segment_segment_distance(a: Segment, b: Segment) -> float:
    from .geometry import segment_to_segment_distance

    return float(segment_to_segment_distance(a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2))


def _ray_exit_extent(pad: Pad, ux: float, uy: float) -> float:
    """Distance from the pad centre to its metal boundary along unit ``(ux, uy)``.

    Uses the pad's own residual-rotation frame (:func:`pad_local_point`) so a
    rotated pad is handled exactly like every other clearance consumer.  Circles
    use their radius; rectangles use the true ray/AABB exit (NOT the support
    function, which would start the stub outside the metal on an oblong pad and
    silently skip the copper in between).
    """
    lx, ly = pad_local_point(pad, pad.x + ux, pad.y + uy)
    if pad.shape == "circle":
        return max(pad.width, pad.height) / 2.0
    half_w, half_h = pad.width / 2.0, pad.height / 2.0
    candidates = []
    if abs(lx) > EPS:
        candidates.append(half_w / abs(lx))
    if abs(ly) > EPS:
        candidates.append(half_h / abs(ly))
    return min(candidates) if candidates else 0.0


def _grid_world_bounds(grid: object) -> tuple[float, float, float, float]:
    origin_x = float(getattr(grid, "origin_x", 0.0))
    origin_y = float(getattr(grid, "origin_y", 0.0))
    resolution = float(getattr(grid, "resolution", 0.1))
    cols = int(getattr(grid, "cols", 0))
    rows = int(getattr(grid, "rows", 0))
    return (
        origin_x,
        origin_y,
        origin_x + max(cols - 1, 0) * resolution,
        origin_y + max(rows - 1, 0) * resolution,
    )


def _round_up_to_cells(value: float, resolution: float) -> float:
    """Smallest whole number of grid cells that is at least ``value``."""
    if resolution <= 0:
        return value
    cells = math.ceil(value / resolution - 1e-9)
    return round(cells * resolution, 6)


# ---------------------------------------------------------------------------
# Raster labelling (descriptive only)
# ---------------------------------------------------------------------------


def _marking_at(grid: object, x: float, y: float, layer_idx: int) -> frozenset[str]:
    """Raster markings at a world point -- used to LABEL, never to decide."""
    marks: set[str] = set()
    try:
        gx, gy = grid.world_to_grid(x, y)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - defensive; grid API is stable
        return frozenset()
    num_layers = int(getattr(grid, "num_layers", 0))
    if not (0 <= layer_idx < num_layers):
        return frozenset()

    static = getattr(grid, "_static_blocked", None)
    if static is not None and bool(static[layer_idx, gy, gx]):
        marks.add("static_blocked")
    pad_blocked = getattr(grid, "_pad_blocked", None)
    if pad_blocked is not None and bool(pad_blocked[layer_idx, gy, gx]):
        marks.add("pad_blocked")
    is_obstacle = getattr(grid, "_is_obstacle", None)
    if is_obstacle is not None and bool(is_obstacle[layer_idx, gy, gx]):
        marks.add("is_obstacle")
    usage = getattr(grid, "_usage_count", None)
    if usage is not None and int(usage[layer_idx, gy, gx]) > 0:
        marks.add("usage_shared")

    reserved = getattr(grid, "_reserved_for_nets", None) or {}
    soft = getattr(grid, "_soft_reservations", None) or set()
    key = (layer_idx, gy, gx)
    if key in reserved:
        marks.add("reserved_soft" if key in soft else "reserved_hard")
    return frozenset(marks)


def _closing_from_pad(grid: object, pad: Pad, *, kind: ClosingKind) -> ClosingCopper:
    """Describe ``pad`` as closing copper.

    The raster is read at the pad centre -- a point guaranteed to be inside the
    pad's own copper, so ``marking`` describes the OBSTACLE rather than the
    rejected candidate.
    """
    half_w, half_h = pad_half_extents(pad)
    layer_idx = _safe_layer_index(grid, pad.layer)
    marking = _marking_at(grid, pad.x, pad.y, layer_idx)
    return ClosingCopper(
        kind=kind,
        net=pad.net,
        net_name=pad.net_name,
        ref=pad.ref,
        pin=pad.pin,
        layer=pad.layer,
        marking=marking,
        shareable=_shareable(marking),
        bbox=(pad.x - half_w, pad.y - half_h, pad.x + half_w, pad.y + half_h),
    )


def _closing_from_segment(
    grid: object,
    route: Route,
    seg: Segment,
    contact: tuple[float, float],
    *,
    kind: ClosingKind,
) -> ClosingCopper:
    layer_idx = _safe_layer_index(grid, seg.layer)
    marking = _marking_at(grid, contact[0], contact[1], layer_idx)
    half = seg.width / 2
    return ClosingCopper(
        kind=kind,
        net=route.net,
        net_name=route.net_name or seg.net_name,
        ref=route.net_name or seg.net_name or f"net{route.net}",
        pin="",
        layer=seg.layer,
        marking=marking,
        shareable=_shareable(marking),
        bbox=(
            min(seg.x1, seg.x2) - half,
            min(seg.y1, seg.y2) - half,
            max(seg.x1, seg.x2) + half,
            max(seg.y1, seg.y2) + half,
        ),
    )


def _closing_from_via(
    grid: object,
    route: Route,
    via: Via,
    *,
    kind: ClosingKind,
) -> ClosingCopper:
    layer_idx = _safe_layer_index(grid, via.layers[0])
    marking = _marking_at(grid, via.x, via.y, layer_idx)
    radius = via.diameter / 2
    return ClosingCopper(
        kind=kind,
        net=route.net,
        net_name=route.net_name or via.net_name,
        ref=route.net_name or via.net_name or f"net{route.net}",
        pin="",
        layer=via.layers[0],
        marking=marking,
        shareable=_shareable(marking),
        bbox=(via.x - radius, via.y - radius, via.x + radius, via.y + radius),
    )


def _shareable(marking: frozenset[str]) -> bool:
    return "usage_shared" in marking and "is_obstacle" not in marking


def _safe_layer_index(grid: object, layer: Layer) -> int:
    try:
        return int(grid.layer_to_index(layer.value))  # type: ignore[attr-defined]
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Attribution -- which copper rejected a candidate
# ---------------------------------------------------------------------------


def _attribute_stub(
    grid: object,
    rules: DesignRules,
    seg: Segment,
    net: int,
    hard_same_net: Sequence[Route | Pad],
) -> list[ClosingCopper]:
    """Name the copper items that are closer to ``seg`` than ``trace_clearance``.

    Attribution is a *labelling* re-scan, never a second opinion: it only runs
    for a candidate the adapter has ALREADY rejected, and it reuses the same
    unchanged ``rules.trace_clearance`` so a named item is genuinely one that
    violates the rule.
    """
    found: list[ClosingCopper] = []
    hard_pads = [item for item in hard_same_net if isinstance(item, Pad)]
    hard_routes = [item for item in hard_same_net if isinstance(item, Route)]
    required = rules.trace_clearance + seg.width / 2

    for pad, is_hard in _iter_obstacle_pads(grid, net, hard_pads):
        if not pad.through_hole and pad.layer != seg.layer:
            continue
        if pad_segment_distance(pad, seg.x1, seg.y1, seg.x2, seg.y2) < required - EPS:
            found.append(
                _closing_from_pad(grid, pad, kind="kelvin_isolated" if is_hard else "foreign_pad")
            )

    for route, is_hard in _iter_obstacle_routes(grid, net, hard_routes):
        for other in route.segments:
            if other.layer != seg.layer:
                continue
            gap = _segment_segment_distance(seg, other) - (seg.width + other.width) / 2
            if gap < rules.trace_clearance - EPS:
                contact = _contact_on_segment(seg, other.x1, other.y1, other.x2, other.y2)
                found.append(
                    _closing_from_segment(
                        grid,
                        route,
                        other,
                        contact,
                        kind="kelvin_isolated" if is_hard else "route_segment",
                    )
                )
        for via in route.vias:
            dist = _point_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)
            if dist - via.diameter / 2 - seg.width / 2 < rules.trace_clearance - EPS:
                found.append(
                    _closing_from_via(
                        grid,
                        route,
                        via,
                        kind="kelvin_isolated" if is_hard else "route_via",
                    )
                )
    return found


def _attribute_via(
    grid: object,
    rules: DesignRules,
    via: Via,
    net: int,
    hard_same_net: Sequence[Route | Pad],
) -> list[ClosingCopper]:
    """Name the copper items that reject ``via`` at the configured clearances."""
    found: list[ClosingCopper] = []
    hard_pads = [item for item in hard_same_net if isinstance(item, Pad)]
    hard_routes = [item for item in hard_same_net if isinstance(item, Route)]
    radius = via.diameter / 2

    for pad, is_hard in _iter_obstacle_pads(grid, net, hard_pads):
        copper_gap = pad_point_distance(pad, via.x, via.y) - radius
        drill_gap = math.inf
        if getattr(pad, "through_hole", False) and float(getattr(pad, "drill", 0.0)) > 0.0:
            drill_gap = (
                math.hypot(via.x - pad.x, via.y - pad.y) - via.drill / 2 - float(pad.drill) / 2
            )
        if copper_gap < rules.via_clearance - EPS or drill_gap < rules.min_hole_to_hole - EPS:
            found.append(
                _closing_from_pad(grid, pad, kind="kelvin_isolated" if is_hard else "foreign_pad")
            )

    for route, is_hard in _iter_obstacle_routes(grid, net, hard_routes):
        for seg in route.segments:
            dist = _point_segment_distance(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)
            if dist - radius - seg.width / 2 < rules.via_clearance - EPS:
                contact = _closest_point_on_segment(via.x, via.y, seg.x1, seg.y1, seg.x2, seg.y2)
                found.append(
                    _closing_from_segment(
                        grid,
                        route,
                        seg,
                        contact,
                        kind="kelvin_isolated" if is_hard else "route_segment",
                    )
                )
        for other in route.vias:
            centre = math.hypot(via.x - other.x, via.y - other.y)
            if centre <= EPS:
                continue
            copper_gap = centre - radius - other.diameter / 2
            drill_gap = centre - via.drill / 2 - other.drill / 2
            if copper_gap < rules.via_clearance - EPS or drill_gap < rules.min_hole_to_hole - EPS:
                found.append(
                    _closing_from_via(
                        grid,
                        route,
                        other,
                        kind="kelvin_isolated" if is_hard else "route_via",
                    )
                )
    return found


def _board_edge_item(bounds: tuple[float, float, float, float]) -> ClosingCopper:
    return ClosingCopper(
        kind="board_edge",
        net=0,
        net_name="",
        ref="<board-edge>",
        pin="",
        layer=Layer.F_CU,
        marking=frozenset(),
        shareable=False,
        bbox=bounds,
    )


def _dedupe(items: Iterable[ClosingCopper]) -> tuple[ClosingCopper, ...]:
    seen: dict[tuple, ClosingCopper] = {}
    for item in items:
        key = (item.kind, item.net, item.ref, item.pin, item.layer.value, item.bbox)
        seen.setdefault(key, item)
    return tuple(sorted(seen.values(), key=lambda item: (item.sort_key, item.bbox)))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def compute_access_set(
    pad: Pad,
    grid: object,
    rules: DesignRules,
    *,
    legality: AccessLegality | None = None,
    trace_width: float | None = None,
    hard_same_net: Sequence[Route | Pad] = (),
) -> AccessSet:
    """Compute the access set of one routing terminal.

    Args:
        pad: The routing terminal.  This is the pad the SEARCH starts from, so
            callers that ran the escape pre-pass must pass the
            ``_escape_pad_overrides`` entry (``escape_terminal=True``) rather
            than the physical pad centre -- except for a Kelvin *root*, whose
            search deliberately uses the physical pad.
        grid: The :class:`~kicad_tools.router.grid.RoutingGrid` holding the
            copper committed so far (``grid._pads`` + ``grid.routes``).  Read
            only; never mutated.
        rules: Design rules.  Clearance values are forwarded to the predicates
            unchanged.
        legality: Legality adapter; defaults to :class:`DefaultAccessLegality`
            built over ``grid``/``rules``/``hard_same_net``.
        trace_width: Width of the exit stubs.  Defaults to
            ``rules.trace_width``; callers with a net-class map pass the class
            width.
        hard_same_net: Same-net copper that must be treated as a hard obstacle
            (Kelvin sibling branches).  Ignored when a custom ``legality`` is
            supplied that does not consult it.

    Returns:
        The :class:`AccessSet`.  ``closing_copper`` is populated only when the
        set is empty.
    """
    width = rules.trace_width if trace_width is None else trace_width
    resolution = float(getattr(grid, "resolution", rules.grid_resolution))
    stub_length = _round_up_to_cells(width + 2 * rules.trace_clearance, resolution)
    adapter: AccessLegality = (
        DefaultAccessLegality(grid, rules, hard_same_net=hard_same_net)
        if legality is None
        else legality
    )
    bounds = _grid_world_bounds(grid)
    origin_layer = pad.layer
    net = pad.net

    rejected_stub_segments: list[Segment] = []
    rejected_via_candidates: list[Via] = []
    out_of_bounds = False

    # -- exit stubs -------------------------------------------------------
    stubs: list[ExitStub] = []
    for direction in DIRECTIONS:
        dx, dy = direction
        norm = math.hypot(dx, dy)
        ux, uy = dx / norm, dy / norm
        start = _ray_exit_extent(pad, ux, uy)
        x0 = round(pad.x + start * ux, 6)
        y0 = round(pad.y + start * uy, 6)
        x1 = round(pad.x + (start + stub_length) * ux, 6)
        y1 = round(pad.y + (start + stub_length) * uy, 6)
        if not _inside(bounds, x1, y1) or not _inside(bounds, x0, y0):
            out_of_bounds = True
            continue
        seg = Segment(
            x1=x0,
            y1=y0,
            x2=x1,
            y2=y1,
            width=width,
            layer=origin_layer,
            net=net,
            net_name=pad.net_name,
        )
        is_legal, _loc = adapter.stub_legal(seg, net)
        if is_legal:
            stubs.append(
                ExitStub(
                    layer=origin_layer,
                    x0=x0,
                    y0=y0,
                    x1=x1,
                    y1=y1,
                    width=width,
                    direction=direction,
                )
            )
        else:
            rejected_stub_segments.append(seg)

    # -- via sites --------------------------------------------------------
    mfr: MfrLimits | None = None
    if rules.manufacturer:
        try:
            mfr = get_mfr_limits(rules.manufacturer)
        except Exception:
            mfr = None
    drill = rules.via_drill if mfr is None else max(rules.via_drill, mfr.min_via_drill)
    diameter = (
        rules.via_diameter
        if mfr is None
        else max(rules.via_diameter, drill + 2 * mfr.min_via_annular)
    )
    other_layers = _other_copper_layers(grid, origin_layer)

    via_candidates: list[tuple[float, float, bool, int | None]] = []
    if mfr is not None and mfr.via_in_pad_supported and not pad.through_hole:
        via_candidates.append((pad.x, pad.y, True, None))
    for index, stub in enumerate(stubs):
        via_candidates.append((stub.x1, stub.y1, False, index))

    via_sites: list[ViaSite] = []
    for x, y, in_pad, from_stub in via_candidates:
        for other_layer in other_layers:
            candidate = Via(
                x=x,
                y=y,
                drill=drill,
                diameter=diameter,
                layers=(origin_layer, other_layer),
                net=net,
                net_name=pad.net_name,
                in_pad=in_pad,
            )
            is_legal, _loc = adapter.via_legal(candidate, net)
            if is_legal:
                via_sites.append(
                    ViaSite(
                        x=x,
                        y=y,
                        drill=drill,
                        diameter=diameter,
                        layers=(origin_layer, other_layer),
                        in_pad=in_pad,
                        from_stub=from_stub,
                    )
                )
            else:
                rejected_via_candidates.append(candidate)

    # -- witness ----------------------------------------------------------
    closing: tuple[ClosingCopper, ...] = ()
    if not stubs and not via_sites:
        items: list[ClosingCopper] = []
        for seg in rejected_stub_segments:
            items.extend(_attribute_stub(grid, rules, seg, net, hard_same_net))
        for candidate in rejected_via_candidates:
            items.extend(_attribute_via(grid, rules, candidate, net, hard_same_net))
        if out_of_bounds:
            items.append(_board_edge_item(bounds))
        if not items and _fixed_fills_reject(grid, rejected_stub_segments, rules):
            items.append(
                ClosingCopper(
                    kind="fixed_fill",
                    net=0,
                    net_name="",
                    ref="<fixed-fill>",
                    pin="",
                    layer=origin_layer,
                    marking=frozenset(),
                    shareable=False,
                    bbox=(pad.x, pad.y, pad.x, pad.y),
                )
            )
        closing = _dedupe(items)

    # -- bbox / fingerprint ----------------------------------------------
    half_w, half_h = pad_half_extents(pad)
    min_x, min_y = pad.x - half_w, pad.y - half_h
    max_x, max_y = pad.x + half_w, pad.y + half_h
    for seg in [*(stub.to_segment(net) for stub in stubs), *rejected_stub_segments]:
        min_x = min(min_x, seg.x1, seg.x2)
        min_y = min(min_y, seg.y1, seg.y2)
        max_x = max(max_x, seg.x1, seg.x2)
        max_y = max(max_y, seg.y1, seg.y2)
    for x, y, _in_pad, _from_stub in via_candidates:
        min_x, min_y = min(min_x, x - diameter / 2), min(min_y, y - diameter / 2)
        max_x, max_y = max(max_x, x + diameter / 2), max(max_y, y + diameter / 2)
    dilation = max(width / 2 + rules.trace_clearance, diameter / 2 + rules.via_clearance)
    bbox = (
        round(min_x - dilation, 6),
        round(min_y - dilation, 6),
        round(max_x + dilation, 6),
        round(max_y + dilation, 6),
    )

    return AccessSet(
        pad_key=pad.key,
        net=net,
        origin=(pad.x, pad.y),
        origin_layer=origin_layer,
        origin_is_escape_terminal=bool(pad.escape_terminal),
        stubs=tuple(stubs),
        via_sites=tuple(via_sites),
        closing_copper=closing,
        bbox=bbox,
        fingerprint=(
            width,
            rules.trace_clearance,
            rules.via_clearance,
            rules.min_hole_to_hole,
            rules.manufacturer,
            int(getattr(grid, "num_layers", 1)),
        ),
    )


def _inside(bounds: tuple[float, float, float, float], x: float, y: float) -> bool:
    return bounds[0] - EPS <= x <= bounds[2] + EPS and bounds[1] - EPS <= y <= bounds[3] + EPS


def _other_copper_layers(grid: object, origin_layer: Layer) -> tuple[Layer, ...]:
    """Routable copper layers other than ``origin_layer``, in stack order."""
    stack = getattr(grid, "layer_stack", None)
    if stack is None:
        return ()
    layers: list[Layer] = []
    for index in stack.get_routable_indices():
        try:
            layer = stack.index_to_layer_enum(index)
        except Exception:  # pragma: no cover - defensive
            continue
        if layer != origin_layer:
            layers.append(layer)
    return tuple(layers)


def _fixed_fills_reject(grid: object, segments: Sequence[Segment], rules: DesignRules) -> bool:
    fills = getattr(grid, "fixed_fills", None)
    if not fills:
        return False
    for seg in segments:
        try:
            clear = fills.segment_clear(
                (seg.x1, seg.y1),
                (seg.x2, seg.y2),
                grid.layer_to_index(seg.layer.value),  # type: ignore[attr-defined]
                seg.width / 2,
                rules.trace_clearance,
            )
        except Exception:  # pragma: no cover - defensive
            return False
        if not clear:
            return True
    return False


def route_envelope(route: Route, rules: DesignRules) -> tuple[float, float, float, float]:
    """Dilated world bbox of everything ``route`` would block if committed.

    Mirrors the envelope ``RoutingGrid.mark_route`` blocks: each segment box
    grown by its half-width, each via by its radius, the whole thing dilated by
    ``max(trace_clearance + trace_width / 2, via_clearance + via_diameter / 2)``.
    """
    min_x = min_y = math.inf
    max_x = max_y = -math.inf
    for seg in route.segments:
        half = seg.width / 2
        min_x = min(min_x, seg.x1 - half, seg.x2 - half)
        min_y = min(min_y, seg.y1 - half, seg.y2 - half)
        max_x = max(max_x, seg.x1 + half, seg.x2 + half)
        max_y = max(max_y, seg.y1 + half, seg.y2 + half)
    for via in route.vias:
        radius = via.diameter / 2
        min_x, min_y = min(min_x, via.x - radius), min(min_y, via.y - radius)
        max_x, max_y = max(max_x, via.x + radius), max(max_y, via.y + radius)
    if min_x is math.inf or min_x > max_x:
        return (0.0, 0.0, 0.0, 0.0)
    dilation = max(
        rules.trace_clearance + rules.trace_width / 2,
        rules.via_clearance + rules.via_diameter / 2,
    )
    return (
        round(min_x - dilation, 6),
        round(min_y - dilation, 6),
        round(max_x + dilation, 6),
        round(max_y + dilation, 6),
    )


def affected_pads(
    access_sets: Mapping[tuple[str, str], AccessSet],
    envelope: tuple[float, float, float, float],
) -> list[tuple[str, str]]:
    """Terminals whose access set a route with ``envelope`` could change.

    **Soundness guarantee**: a key that is NOT returned has an access set that
    is provably unchanged by committing that route -- every predicate behind
    :class:`AccessLegality` is local to the dilated geometry, so copper entirely
    outside a terminal's dilated access bbox cannot flip any of its candidates.
    False positives are allowed (and cheap): this is a closed-interval bbox
    overlap test, O(len(access_sets)), no spatial index.
    """
    e_min_x, e_min_y, e_max_x, e_max_y = envelope
    hits: list[tuple[str, str]] = []
    for key, access in access_sets.items():
        b_min_x, b_min_y, b_max_x, b_max_y = access.bbox
        if (
            b_max_x < e_min_x - EPS
            or b_min_x > e_max_x + EPS
            or b_max_y < e_min_y - EPS
            or b_min_y > e_max_y + EPS
        ):
            continue
        hits.append(key)
    return sorted(hits)
