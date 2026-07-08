"""
Basic data structures for PCB routing.

This module provides:
- Point: 3D coordinate in routing space
- GridCell: Cell in the routing grid with congestion tracking
- Via: Layer transition point
- Segment: Trace segment between two points
- Route: Complete path with segments and vias
- Pad: Component pad to connect
- Obstacle: Area to avoid during routing
"""

import dataclasses
import random
import uuid
from dataclasses import dataclass, field

from .layers import Layer

# Issue #3272: Deterministic UUID mode.  When :func:`enable_deterministic_uuids`
# is called the per-element UUID emitted by :meth:`Segment.to_sexp` and
# :meth:`Via.to_sexp` is derived from the seeded global ``random`` module
# instead of ``os.urandom`` (which ``uuid.uuid4`` consults).  This makes the
# routed ``.kicad_pcb`` byte-identical across runs that share a routing seed
# -- the smoke harness at ``scripts/ci/board06_determinism_smoke.sh`` and the
# regression test at ``tests/router/test_board06_determinism.py`` rely on
# this property.  ``route_all_negotiated`` and ``route_all_with_diffpairs``
# turn this on whenever ``seed is not None``; callers that need
# stochastic UUIDs (e.g. unit tests asserting uniqueness across calls)
# can leave the toggle off (the default).
_DETERMINISTIC_UUIDS: bool = False


def enable_deterministic_uuids(enabled: bool = True) -> None:
    """Toggle deterministic UUID emission for :class:`Segment` and :class:`Via`.

    Issue #3272.  When ``enabled`` is True, ``to_sexp()`` derives each
    UUID from ``random.getrandbits(128)`` (seeded by the caller via
    ``random.seed(seed)``) instead of ``uuid.uuid4()`` (which reads
    ``os.urandom`` and is therefore non-deterministic).  The resulting
    UUID still has the canonical xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    shape so KiCad and downstream tools see the same format.

    This is a module-level toggle rather than a per-instance argument
    because the emit sites live deep in router internals and threading
    a flag through every call site would touch every place that
    instantiates a Segment/Via.  The toggle is meant to be flipped on at
    the top of a deterministic route session and flipped off again at
    the end.  See :func:`reset_deterministic_uuids`.
    """
    global _DETERMINISTIC_UUIDS
    _DETERMINISTIC_UUIDS = bool(enabled)


def reset_deterministic_uuids() -> None:
    """Restore default (non-deterministic) UUID emission.  Issue #3272."""
    global _DETERMINISTIC_UUIDS
    _DETERMINISTIC_UUIDS = False


def is_deterministic_uuids_enabled() -> bool:
    """Return the current :func:`enable_deterministic_uuids` state.

    Exposed for tests and diagnostics that need to assert / branch on
    the deterministic-UUID mode without reaching into module privates.
    """
    return _DETERMINISTIC_UUIDS


def _make_uuid() -> str:
    """Generate a UUID string honoring the :func:`enable_deterministic_uuids` toggle.

    When deterministic mode is active the UUID is derived from
    ``random.getrandbits(128)`` so it tracks the seeded global RNG.
    Otherwise we fall through to ``uuid.uuid4()`` which is what the
    pre-#3272 code emitted.
    """
    if _DETERMINISTIC_UUIDS:
        return str(uuid.UUID(int=random.getrandbits(128), version=4))
    return str(uuid.uuid4())


def _fmt(val: float) -> int | float:
    """Format float with 4 decimal precision for PCB output.

    Rounds to 4 decimal places (0.1 micron precision in mm).
    Returns int if no fractional part for cleaner output.
    """
    rounded = round(val, 4)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


@dataclass
class Point:
    """A point in 3D routing space (x, y, layer)."""

    x: float
    y: float
    layer: Layer = Layer.F_CU

    def __hash__(self) -> int:
        return hash((round(self.x, 4), round(self.y, 4), self.layer))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Point):
            return NotImplemented
        return (
            round(self.x, 4) == round(other.x, 4)
            and round(self.y, 4) == round(other.y, 4)
            and self.layer == other.layer
        )

    def grid_key(self, resolution: float) -> tuple[int, int, int]:
        """Get grid cell key."""
        return (
            round(self.x / resolution),
            round(self.y / resolution),
            self.layer.value,
        )

    def distance_to(self, other: "Point") -> float:
        """Manhattan distance (same layer) or Euclidean."""
        if self.layer == other.layer:
            return abs(self.x - other.x) + abs(self.y - other.y)
        else:
            # Include via cost estimate
            return (
                abs(self.x - other.x)
                + abs(self.y - other.y)
                + abs(self.layer.value - other.layer.value) * 0.5
            )


@dataclass
class GridCell:
    """A cell in the routing grid with negotiated congestion support."""

    x: int
    y: int
    layer: int
    blocked: bool = False
    net: int = 0  # 0 = empty, >0 = assigned to net
    cost: float = 1.0  # Routing cost multiplier
    # Negotiated congestion fields
    usage_count: int = 0  # How many nets currently use this cell
    history_cost: float = 0.0  # Accumulated congestion from previous iterations
    is_obstacle: bool = False  # True for pads/keepouts (never allow sharing)
    # Zone fields for copper pour support
    is_zone: bool = False  # True if cell is part of a copper pour zone
    zone_id: str | None = None  # UUID of the zone (for multi-zone layers)
    # Pad ownership tracking (prevents route rip-up from corrupting pad cells)
    pad_blocked: bool = False  # True if blocked by a pad (not a route)
    original_net: int = 0  # Net that first claimed this cell (for restoration)


@dataclass
class Via:
    """A via connecting layers."""

    x: float
    y: float
    drill: float
    diameter: float
    layers: tuple[Layer, Layer]
    net: int = 0
    net_name: str = ""
    # Issue #2605: in-pad escape via marker.  When True, this via was
    # placed dead-centre on a fine-pitch SMD pad as part of escape routing.
    # The pad's own copper provides the annular ring, so segment-to-pad
    # clearance checks against the parent pad must be exempted.  KiCad's
    # board file does not need a separate fill/plating attribute -- the
    # manufacturer reads via-in-pad from the order options / DRU.
    in_pad: bool = False
    # Issue #3124 (folds in #3118 prerequisite): micro-via marker.  When
    # True, this via is serialized as ``(via micro ...)`` so KiCad and
    # board manufacturers know to treat it as a laser-drilled micro-via
    # rather than a standard through-hole via.  Matches
    # :func:`kicad_tools.sexp.builders.via_node`'s ``via_type="micro"``
    # output exactly.  The router's in-pad escape (#3118) is the primary
    # producer of micro vias from inside the routing pipeline.
    is_micro: bool = False

    def to_sexp(self) -> str:
        """Generate KiCad S-expression.

        Emits ``(via micro ...)`` when :attr:`is_micro` is True so the
        micro-via token survives the route -> finalize -> file
        round-trip (issue #3124).  Otherwise emits a plain ``(via ...)``.
        """
        layer_start = self.layers[0].kicad_name
        layer_end = self.layers[1].kicad_name
        type_token = " micro" if self.is_micro else ""
        # Issue #3925: field order matches KiCad's canonical writer
        # (uuid BEFORE net).  Emitting net before uuid caused every via to
        # churn on the first KiCad open/save round-trip, producing a diff
        # proportional to via count even when no geometry changed.
        return f"""(via{type_token}
\t\t(at {self.x:.4f} {self.y:.4f})
\t\t(size {_fmt(self.diameter)})
\t\t(drill {_fmt(self.drill)})
\t\t(layers "{layer_start}" "{layer_end}")
\t\t(uuid "{_make_uuid()}")
\t\t(net {self.net})
\t)"""


@dataclass
class Segment:
    """A trace segment."""

    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: Layer
    net: int = 0
    net_name: str = ""

    @property
    def start(self) -> tuple[float, float]:
        """Return the start point as a tuple (x1, y1)."""
        return (self.x1, self.y1)

    @property
    def end(self) -> tuple[float, float]:
        """Return the end point as a tuple (x2, y2)."""
        return (self.x2, self.y2)

    def to_sexp(self) -> str:
        """Generate KiCad S-expression.

        Issue #3925: field order matches KiCad's canonical writer (uuid
        BEFORE net).  Emitting net before uuid caused every segment to
        churn on the first KiCad open/save round-trip, producing a diff
        proportional to segment count even when no geometry changed.
        """
        return f"""(segment
\t\t(start {self.x1:.4f} {self.y1:.4f})
\t\t(end {self.x2:.4f} {self.y2:.4f})
\t\t(width {_fmt(self.width)})
\t\t(layer "{self.layer.kicad_name}")
\t\t(uuid "{_make_uuid()}")
\t\t(net {self.net})
\t)"""


@dataclass
class Route:
    """A complete route between two points."""

    net: int
    net_name: str
    segments: list[Segment] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    # Issue #3441: True for sub-grid escape stubs (#1603) emitted by the
    # pre-pass.  An escape stub only connects an off-grid pad to its
    # nearest grid point -- it is NOT a full net route, so the negotiated
    # loop's pre-routed-net filter (#2464) must not treat nets that have
    # only escape stubs as already routed (doing so left board 07's six
    # TMDS nets permanently at 1/2 pads connected when the pre-pass was
    # re-enabled under the C++ backend).
    is_escape: bool = False

    def to_sexp(self) -> str:
        """Generate all S-expressions for this route."""
        parts = []
        for seg in self.segments:
            parts.append(seg.to_sexp())
        for via in self.vias:
            parts.append(via.to_sexp())
        return "\n\t".join(parts)

    def copy_geometry(self) -> "Route":
        """Return a geometric snapshot of this route (Issue #3507).

        Produces a new :class:`Route` whose :class:`Segment` and
        :class:`Via` objects are field-level copies of this route's, so
        the snapshot's geometry survives in-place mutation of the
        original (the ``drc_verify_and_nudge`` pass nudges segment
        coordinates and merges vias directly on the live objects).
        Used by :meth:`RoutingGrid.resync_route_occupancy` to unmark the
        PRE-mutation copper from the routing grid after a post-route
        pass mutates route geometry.
        """
        return Route(
            net=self.net,
            net_name=self.net_name,
            segments=[dataclasses.replace(seg) for seg in self.segments],
            vias=[dataclasses.replace(via) for via in self.vias],
            is_escape=self.is_escape,
        )

    def validate_layer_transitions(
        self,
        via_drill: float = 0.35,
        via_diameter: float = 0.7,
    ) -> int:
        """Ensure all layer transitions have corresponding vias.

        When consecutive segments are on different layers, there must be a via
        at the transition point. This method detects missing vias and inserts
        them to ensure electrically valid routes.

        Args:
            via_drill: Drill diameter for inserted vias (mm)
            via_diameter: Total diameter for inserted vias (mm)

        Returns:
            Number of vias inserted
        """
        if len(self.segments) < 2:
            return 0

        vias_inserted = 0

        for i in range(len(self.segments) - 1):
            seg1 = self.segments[i]
            seg2 = self.segments[i + 1]

            if seg1.layer != seg2.layer:
                # Layer transition - check for via at connection point
                # Segments connect at seg1.end == seg2.start
                transition_x = seg1.x2
                transition_y = seg1.y2

                # Check if via already exists at this point
                has_via = any(
                    abs(via.x - transition_x) < 0.01 and abs(via.y - transition_y) < 0.01
                    for via in self.vias
                )

                if not has_via:
                    # Insert missing via
                    new_via = Via(
                        x=transition_x,
                        y=transition_y,
                        drill=via_drill,
                        diameter=via_diameter,
                        layers=(seg1.layer, seg2.layer),
                        net=self.net,
                        net_name=self.net_name,
                    )
                    self.vias.append(new_via)
                    vias_inserted += 1

        return vias_inserted


@dataclass
class Pad:
    """A pad to connect."""

    x: float
    y: float
    width: float
    height: float
    net: int
    net_name: str
    layer: Layer = Layer.F_CU
    ref: str = ""  # Component reference
    pin: str = ""  # Pin number/name
    through_hole: bool = False  # PTH pads block both layers
    drill: float = 0.0  # Drill diameter for PTH pads (0 = use pad size)
    steiner_point: bool = False  # True for virtual Steiner tree branch points
    footprint_name: str = ""  # Library footprint name, e.g. "Package_QFP:TQFP-32_7x7mm_P0.8mm"


@dataclass
class Obstacle:
    """An obstacle to avoid."""

    x: float
    y: float
    width: float
    height: float
    layer: Layer
    clearance: float = 0.0
