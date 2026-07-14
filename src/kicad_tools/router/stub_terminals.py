"""Boundary stub-terminal detection for region-bounded routing (Phase 2b-0).

When ``pcb strip --region`` clips traces at a routing-region boundary
(``schema/pcb.py`` boundary-crossing branch, lines ~5200-5227), every
boundary-crossing segment is rewritten so that its *inside* endpoint moves
onto the region boundary while its *outside* endpoint is retained unchanged.
The result is a bare copper "stub" that runs boundary -> outside with no pad
at the boundary end. Phase 2a's inclusive-box routing has no abstraction for
these bare stub endpoints, so nets that own pads both inside and outside the
region cannot be reconnected to the pre-existing external copper.

This module provides the *single shared producer* that both pad-sourcing
surfaces (``Autorouter`` and ``RoutingOrchestrator``) will consume in later
phases (#4170 / #4173), so stub geometry is never re-derived independently
(the #3428 foot-gun). It is intentionally **pure**: it holds no grid or router
state and takes all segment/pad/net data as plain arguments.

The clipped-geometry UUID map that ``strip_traces`` builds is ephemeral and is
not persisted on ``PCB``, so this detector deliberately re-derives stub
endpoints from *loaded* segments rather than querying the PCB for "which
segments are stubs" -- this is by design (see the note in #4172 / #4170).

``StubTerminal`` objects are **route-scoped and ephemeral**: they are never a
``Pad`` and must never be inserted into ``Autorouter.pads`` / ``nets``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from kicad_tools.core.types import CopperLayer

# Proximity tolerance in millimeters. Mirrors the local ``EPSILON = 0.01``
# used by ``core.py::_index_block_internal_traces`` (defined there as a
# function-local variable, so it cannot be imported by name). Matching the
# value and its semantic role -- pad/endpoint proximity tolerance -- keeps the
# stub detector consistent with the router's existing coincidence checks.
EPSILON = 0.01


class BoundaryEdge(Enum):
    """Which edge of the rectangular routing region a stub endpoint lies on.

    Recorded on :class:`StubTerminal` for the detector's own correctness
    checks and downstream diagnostics; it is not a routing input.
    """

    LEFT = "left"
    RIGHT = "right"
    TOP = "top"
    BOTTOM = "bottom"


@dataclass(frozen=True)
class RegionBox:
    """Axis-aligned routing region in board-relative millimeters.

    Endpoints are classified against ``[x1, x2] x [y1, y2]``. The box is
    normalized on construction so ``x1 <= x2`` and ``y1 <= y2`` regardless of
    the order the corners are supplied in.
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x1 > self.x2:
            lo, hi = self.x2, self.x1
            object.__setattr__(self, "x1", lo)
            object.__setattr__(self, "x2", hi)
        if self.y1 > self.y2:
            lo, hi = self.y2, self.y1
            object.__setattr__(self, "y1", lo)
            object.__setattr__(self, "y2", hi)

    def contains(self, x: float, y: float) -> bool:
        """Inclusive membership test (matches Phase 2a's inclusive box)."""
        return self.x1 <= x <= self.x2 and self.y1 <= y <= self.y2

    def contains_strict(self, x: float, y: float, epsilon: float = EPSILON) -> bool:
        """Strictly-inside test: inside the box and not on/near any edge.

        Used to decide whether the *other* endpoint of a stub segment is
        strictly OUTSIDE the region. An endpoint sitting on the boundary line
        (within ``epsilon``) is treated as on-boundary, not strictly inside.
        """
        return (
            self.x1 + epsilon < x < self.x2 - epsilon and self.y1 + epsilon < y < self.y2 - epsilon
        )


@dataclass(frozen=True)
class StubSegment:
    """A loaded copper segment, reduced to what the detector needs.

    Callers (``Autorouter`` / ``RoutingOrchestrator``) adapt their own segment
    representation -- e.g. ``schema.pcb.Segment`` or ``router.primitives.Segment``
    -- into this plain-data shape so the detector stays router-agnostic and
    pure. Coordinates are board-relative millimeters.
    """

    net_id: int
    net_name: str
    x1: float
    y1: float
    x2: float
    y2: float
    layer: CopperLayer
    uuid: str | None = None


@dataclass(frozen=True)
class PadLocation:
    """Center of a pad or via, used only for coincidence rejection.

    A stub endpoint that coincides (within :data:`EPSILON`) with any pad/via
    center routes "for free" under Phase 2a's inclusive box test and is not a
    stub terminal.
    """

    net_id: int
    x: float
    y: float


@dataclass
class StubTerminal:
    """A bare copper stub endpoint on the region boundary to be reconnected.

    Route-scoped and ephemeral: NEVER a ``Pad``, and never inserted into
    ``Autorouter.pads`` / ``nets``. ``source_segment_uuid`` and
    ``boundary_edge`` are provenance for the detector's own correctness checks
    and downstream diagnostics.
    """

    net_id: int
    net_name: str
    x: float
    y: float
    layer: CopperLayer
    source_segment_uuid: str | None = None
    boundary_edge: BoundaryEdge | None = field(default=None)


def _edge_for_point(x: float, y: float, region: RegionBox, epsilon: float) -> BoundaryEdge | None:
    """Return the region edge a point lies on, or ``None``.

    A point is "on the boundary line" when it is within ``epsilon`` of one of
    the four edge lines AND within the box's extent along that edge (so a point
    far off the end of an edge line does not count). When a point is near a
    corner it may satisfy two edges; the nearer edge is chosen deterministically.
    """
    # Must be within the (epsilon-expanded) box extent to be "on" the boundary.
    if not (
        region.x1 - epsilon <= x <= region.x2 + epsilon
        and region.y1 - epsilon <= y <= region.y2 + epsilon
    ):
        return None

    # Distance to each edge line, considering only edges whose span the point
    # falls within (with epsilon slack at the corners).
    candidates: list[tuple[float, BoundaryEdge]] = []
    on_x_span = region.x1 - epsilon <= x <= region.x2 + epsilon
    on_y_span = region.y1 - epsilon <= y <= region.y2 + epsilon
    if on_y_span:
        candidates.append((abs(x - region.x1), BoundaryEdge.LEFT))
        candidates.append((abs(x - region.x2), BoundaryEdge.RIGHT))
    if on_x_span:
        candidates.append((abs(y - region.y1), BoundaryEdge.TOP))
        candidates.append((abs(y - region.y2), BoundaryEdge.BOTTOM))

    best: tuple[float, BoundaryEdge] | None = None
    for dist, edge in candidates:
        if dist <= epsilon and (best is None or dist < best[0]):
            best = (dist, edge)
    return best[1] if best is not None else None


def _coincident_with_pad(x: float, y: float, pads: list[PadLocation], epsilon: float) -> bool:
    """True if ``(x, y)`` is within ``epsilon`` of any pad/via center."""
    eps_sq = epsilon * epsilon
    for pad in pads:
        dx = pad.x - x
        dy = pad.y - y
        if dx * dx + dy * dy <= eps_sq:
            return True
    return False


def detect_boundary_stub_terminals(
    segments: list[StubSegment],
    pads: list[PadLocation],
    region: RegionBox,
    *,
    epsilon: float = EPSILON,
) -> dict[int, list[StubTerminal]]:
    """Detect bare boundary stub endpoints that need reconnection.

    Pure function -- no grid, no router, no ``PCB`` state. Both ``Autorouter``
    and ``RoutingOrchestrator`` can call it identically by adapting their data
    into :class:`StubSegment` / :class:`PadLocation`.

    A segment endpoint is a boundary stub terminal when ALL of the following
    hold (the four-part detection spec from the #4170 design):

    1. The endpoint lies on the region-boundary line within ``epsilon``.
    2. The segment's OTHER endpoint is strictly OUTSIDE the region (surviving
       clipped stubs always run boundary -> outside).
    3. The endpoint is NOT coincident (within ``epsilon``) with any pad/via
       center (such endpoints route "for free" under Phase 2a's inclusive box).
    4. The endpoint's net has pad(s) OUTSIDE the region AND at least one pad
       INSIDE it (exactly the nets Phase 2a currently fails to reconnect).

    Args:
        segments: Loaded copper segments (board-relative mm), adapted to
            :class:`StubSegment`.
        pads: Pad/via centers (board-relative mm), adapted to
            :class:`PadLocation`. Used both for coincidence rejection (part 3)
            and for the per-net inside/outside census (part 4).
        region: The routing region box (board-relative mm).
        epsilon: Proximity tolerance in mm (defaults to :data:`EPSILON`).

    Returns:
        Mapping of ``net_id -> list[StubTerminal]``. Nets with no detected
        stub terminals are omitted. Terminals are returned in input-segment
        order for determinism.
    """
    # Part 4 precondition: per-net census of pads inside vs. outside the region.
    # A pad exactly on the boundary counts as inside (inclusive box), matching
    # Phase 2a semantics.
    nets_with_pad_inside: set[int] = set()
    nets_with_pad_outside: set[int] = set()
    for pad in pads:
        if region.contains(pad.x, pad.y):
            nets_with_pad_inside.add(pad.net_id)
        else:
            nets_with_pad_outside.add(pad.net_id)
    eligible_nets = nets_with_pad_inside & nets_with_pad_outside

    result: dict[int, list[StubTerminal]] = {}

    for seg in segments:
        # Part 4: only nets that straddle the boundary can produce a stub that
        # needs reconnection.
        if seg.net_id not in eligible_nets:
            continue

        # Evaluate both endpoints; a clipped stub places the boundary end at
        # exactly one endpoint, with the other strictly outside.
        for bx, by, ox, oy in (
            (seg.x1, seg.y1, seg.x2, seg.y2),
            (seg.x2, seg.y2, seg.x1, seg.y1),
        ):
            # Part 1: boundary endpoint on the region-boundary line.
            edge = _edge_for_point(bx, by, region, epsilon)
            if edge is None:
                continue

            # Part 2: the OTHER endpoint must be strictly outside the region.
            if region.contains(ox, oy) or _edge_for_point(ox, oy, region, epsilon):
                # Other endpoint is inside or itself on the boundary -> not a
                # boundary -> outside stub.
                continue

            # Part 3: boundary endpoint not coincident with any pad/via center.
            if _coincident_with_pad(bx, by, pads, epsilon):
                continue

            terminal = StubTerminal(
                net_id=seg.net_id,
                net_name=seg.net_name,
                x=bx,
                y=by,
                layer=seg.layer,
                source_segment_uuid=seg.uuid,
                boundary_edge=edge,
            )
            result.setdefault(seg.net_id, []).append(terminal)

    return result
