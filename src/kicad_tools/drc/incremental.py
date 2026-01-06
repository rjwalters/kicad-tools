"""Incremental DRC engine for real-time design validation.

This module provides an incremental DRC engine that can efficiently validate
changes without re-checking the entire board. It uses R-tree spatial indexing
for fast region queries and caches DRC state between checks.

The main class is IncrementalDRC which provides:
- full_check(): Perform full DRC and cache state
- check_move(): Check DRC impact of moving a component (preview)
- apply_move(): Apply move and update cached state

Example:
    >>> from kicad_tools.drc.incremental import IncrementalDRC
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.manufacturers import get_profile
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> rules = get_profile("jlcpcb").get_design_rules()
    >>> drc = IncrementalDRC(pcb, rules)
    >>>
    >>> # Full initial check
    >>> violations = drc.full_check()
    >>> print(f"Found {len(violations)} violations")
    >>>
    >>> # Preview moving U1 to new position
    >>> delta = drc.check_move("U1", new_x=50.0, new_y=30.0)
    >>> print(f"Move would create {len(delta.new_violations)} new violations")
    >>> print(f"Move would resolve {len(delta.resolved_violations)} violations")
    >>>
    >>> # Apply the move and update state
    >>> delta = drc.apply_move("U1", new_x=50.0, new_y=30.0)
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules
    from kicad_tools.schema.pcb import PCB, Footprint

# Try to import rtree, gracefully handle missing dependency
try:
    from rtree import index as rtree_index

    RTREE_AVAILABLE = True
except ImportError:
    RTREE_AVAILABLE = False
    rtree_index = None  # type: ignore[assignment]


@dataclass
class Rectangle:
    """Axis-aligned bounding box for spatial queries."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def center_x(self) -> float:
        """X coordinate of center."""
        return (self.min_x + self.max_x) / 2

    @property
    def center_y(self) -> float:
        """Y coordinate of center."""
        return (self.min_y + self.max_y) / 2

    @property
    def width(self) -> float:
        """Width of rectangle."""
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        """Height of rectangle."""
        return self.max_y - self.min_y

    def translate(self, dx: float, dy: float) -> Rectangle:
        """Return a new rectangle translated by (dx, dy)."""
        return Rectangle(
            min_x=self.min_x + dx,
            min_y=self.min_y + dy,
            max_x=self.max_x + dx,
            max_y=self.max_y + dy,
        )

    def union(self, other: Rectangle) -> Rectangle:
        """Return the bounding box containing both rectangles."""
        return Rectangle(
            min_x=min(self.min_x, other.min_x),
            min_y=min(self.min_y, other.min_y),
            max_x=max(self.max_x, other.max_x),
            max_y=max(self.max_y, other.max_y),
        )

    def expand(self, margin: float) -> Rectangle:
        """Return a new rectangle expanded by margin on all sides."""
        return Rectangle(
            min_x=self.min_x - margin,
            min_y=self.min_y - margin,
            max_x=self.max_x + margin,
            max_y=self.max_y + margin,
        )

    def intersects(self, other: Rectangle) -> bool:
        """Check if this rectangle intersects with another."""
        return not (
            self.max_x < other.min_x
            or self.min_x > other.max_x
            or self.max_y < other.min_y
            or self.min_y > other.max_y
        )

    def as_tuple(self) -> tuple[float, float, float, float]:
        """Return as (min_x, min_y, max_x, max_y) tuple for rtree."""
        return (self.min_x, self.min_y, self.max_x, self.max_y)

    @classmethod
    def from_center(cls, cx: float, cy: float, width: float, height: float) -> Rectangle:
        """Create rectangle from center point and dimensions."""
        hw, hh = width / 2, height / 2
        return cls(cx - hw, cy - hh, cx + hw, cy + hh)


@dataclass
class Violation:
    """A DRC violation with location and involved items.

    Attributes:
        rule_id: Identifier for the violated rule (e.g., "clearance")
        message: Human-readable description of the violation
        severity: Severity level ("error", "warning", "info")
        location: (x, y) position in mm where violation occurs
        layer: PCB layer where violation occurs
        items: References of items involved (e.g., ["U1", "R3-1"])
        nets: Net names involved in the violation
        actual_value: Actual measured value (e.g., clearance in mm)
        required_value: Required value per design rules
    """

    rule_id: str
    message: str
    severity: str = "error"
    location: tuple[float, float] = (0.0, 0.0)
    layer: str = ""
    items: tuple[str, ...] = ()
    nets: tuple[str, ...] = ()
    actual_value: float | None = None
    required_value: float | None = None

    def involves(self, ref: str) -> bool:
        """Check if this violation involves the given component reference."""
        return any(item.startswith(ref) for item in self.items)

    def __hash__(self) -> int:
        """Hash based on rule, location, and items for deduplication."""
        return hash((self.rule_id, self.location, self.items))

    def __eq__(self, other: object) -> bool:
        """Equality based on rule, location, and items."""
        if not isinstance(other, Violation):
            return False
        return (
            self.rule_id == other.rule_id
            and self.location == other.location
            and self.items == other.items
        )


class SpatialIndex:
    """R-tree spatial index for fast region queries.

    Uses the rtree library for O(log n) spatial queries instead of O(n)
    linear scans. Falls back to linear scan if rtree is not installed.

    Example:
        >>> index = SpatialIndex()
        >>> index.insert("U1", Rectangle(10, 10, 20, 20))
        >>> index.insert("R1", Rectangle(15, 15, 25, 25))
        >>> nearby = index.query(Rectangle(12, 12, 18, 18))
        >>> print(nearby)  # ["U1", "R1"]
    """

    def __init__(self) -> None:
        """Initialize the spatial index."""
        self._id_map: dict[str, int] = {}
        self._reverse_map: dict[int, str] = {}
        self._bounds: dict[str, Rectangle] = {}
        self._next_id = 0

        if RTREE_AVAILABLE:
            # Use in-memory index for best performance
            p = rtree_index.Property()
            p.dimension = 2
            self._index: rtree_index.Index | None = rtree_index.Index(properties=p)
        else:
            self._index = None

    def insert(self, ref: str, bounds: Rectangle) -> None:
        """Insert an item into the spatial index.

        Args:
            ref: Reference identifier for the item (e.g., "U1", "R3")
            bounds: Bounding box of the item
        """
        # Remove existing entry if present
        if ref in self._id_map:
            self.remove(ref)

        idx = self._next_id
        self._next_id += 1
        self._id_map[ref] = idx
        self._reverse_map[idx] = ref
        self._bounds[ref] = bounds

        if self._index is not None:
            self._index.insert(idx, bounds.as_tuple())

    def remove(self, ref: str) -> None:
        """Remove an item from the spatial index.

        Args:
            ref: Reference identifier to remove
        """
        if ref not in self._id_map:
            return

        idx = self._id_map[ref]
        bounds = self._bounds[ref]

        if self._index is not None:
            self._index.delete(idx, bounds.as_tuple())

        del self._id_map[ref]
        del self._reverse_map[idx]
        del self._bounds[ref]

    def query(self, bounds: Rectangle) -> list[str]:
        """Find all items intersecting the given bounds.

        Args:
            bounds: Query region

        Returns:
            List of reference identifiers for items in the region
        """
        if self._index is not None:
            ids = list(self._index.intersection(bounds.as_tuple()))
            return [self._reverse_map[i] for i in ids if i in self._reverse_map]
        else:
            # Fallback to linear scan
            result = []
            for ref, item_bounds in self._bounds.items():
                if bounds.intersects(item_bounds):
                    result.append(ref)
            return result

    def update(self, ref: str, new_bounds: Rectangle) -> None:
        """Update the bounds of an existing item.

        Args:
            ref: Reference identifier
            new_bounds: New bounding box
        """
        if ref in self._id_map:
            self.remove(ref)
        self.insert(ref, new_bounds)

    def get_bounds(self, ref: str) -> Rectangle | None:
        """Get the stored bounds for an item.

        Args:
            ref: Reference identifier

        Returns:
            Rectangle bounds or None if not found
        """
        return self._bounds.get(ref)

    def __len__(self) -> int:
        """Return the number of items in the index."""
        return len(self._id_map)

    def __contains__(self, ref: str) -> bool:
        """Check if a reference is in the index."""
        return ref in self._id_map


@dataclass
class DRCState:
    """Cached DRC state for incremental updates.

    Stores the current DRC state including violations, spatial index,
    and component/net information for efficient incremental checks.

    Attributes:
        violations: Current list of DRC violations
        spatial_index: R-tree index for fast spatial queries
        component_bounds: Bounding boxes for each component
        net_segments: Trace segments grouped by net
        last_full_check: Timestamp of last full DRC check
    """

    violations: list[Violation] = field(default_factory=list)
    spatial_index: SpatialIndex = field(default_factory=SpatialIndex)
    component_bounds: dict[str, Rectangle] = field(default_factory=dict)
    net_segments: dict[str, list[tuple[float, float, float, float]]] = field(default_factory=dict)
    last_full_check: datetime = field(default_factory=datetime.now)


@dataclass
class DRCDelta:
    """Changes in DRC state after an operation.

    Represents the difference in DRC violations after a change such as
    moving a component. Used for previewing changes before applying them.

    Attributes:
        new_violations: Violations introduced by the change
        resolved_violations: Existing violations fixed by the change
        affected_components: Component references affected by the change
        affected_nets: Net names affected by the change
        check_time_ms: Time taken for the incremental check in milliseconds
    """

    new_violations: list[Violation] = field(default_factory=list)
    resolved_violations: list[Violation] = field(default_factory=list)
    affected_components: list[str] = field(default_factory=list)
    affected_nets: list[str] = field(default_factory=list)
    check_time_ms: float = 0.0

    @property
    def net_change(self) -> int:
        """Net change in violation count (positive = more violations)."""
        return len(self.new_violations) - len(self.resolved_violations)

    @property
    def is_improvement(self) -> bool:
        """True if the change reduces violations."""
        return self.net_change < 0

    def summary(self) -> str:
        """Return a human-readable summary of the delta."""
        if self.net_change > 0:
            return f"+{self.net_change} violations ({len(self.new_violations)} new, {len(self.resolved_violations)} resolved)"
        elif self.net_change < 0:
            return f"{self.net_change} violations ({len(self.new_violations)} new, {len(self.resolved_violations)} resolved)"
        else:
            return f"No net change ({len(self.new_violations)} new, {len(self.resolved_violations)} resolved)"


class IncrementalDRC:
    """DRC engine with incremental update capability.

    Provides efficient DRC checking by caching state and only re-checking
    affected areas when components move. Uses R-tree spatial indexing for
    O(log n) region queries.

    Performance targets:
        - 50 components: full <100ms, incremental <5ms
        - 200 components: full <500ms, incremental <10ms
        - 500 components: full <2s, incremental <20ms

    Example:
        >>> drc = IncrementalDRC(pcb, rules)
        >>> violations = drc.full_check()
        >>> delta = drc.check_move("U1", 50.0, 30.0)  # Preview
        >>> delta = drc.apply_move("U1", 50.0, 30.0)  # Apply
    """

    def __init__(self, pcb: PCB, rules: DesignRules) -> None:
        """Initialize the incremental DRC engine.

        Args:
            pcb: The PCB to check
            rules: Design rules from manufacturer profile
        """
        self.pcb = pcb
        self.rules = rules
        self.state: DRCState | None = None
        self._component_nets: dict[str, list[str]] = {}
        self._max_clearance = self._compute_max_clearance()

    def _compute_max_clearance(self) -> float:
        """Compute the maximum clearance that needs to be checked."""
        # Use the minimum clearance as the check distance
        # We expand query regions by this amount to catch all potential violations
        return self.rules.min_clearance_mm

    def full_check(self) -> list[Violation]:
        """Perform full DRC and cache state.

        Runs a complete DRC check on the entire board and caches the
        results for subsequent incremental checks.

        Returns:
            List of all DRC violations found
        """
        start_time = time.perf_counter()

        # Initialize state
        self.state = DRCState()

        # Build spatial index and component bounds
        self._build_spatial_index()

        # Build net-to-component mapping
        self._build_net_mapping()

        # Extract net segments
        self._extract_net_segments()

        # Run all checks
        violations = self._check_all()
        self.state.violations = violations
        self.state.last_full_check = datetime.now()

        elapsed_ms = (time.perf_counter() - start_time) * 1000
        # Store timing for debugging (not exposed in API)
        self._last_full_check_ms = elapsed_ms

        return violations

    def check_move(self, ref: str, new_x: float, new_y: float) -> DRCDelta:
        """Check DRC impact of moving a component.

        Performs an incremental check to determine what violations would
        be introduced or resolved by moving a component. Does not modify
        the cached state.

        Only checks:
        1. New position for clearance violations
        2. Components in the affected area
        3. Nets connected to the moved component

        Args:
            ref: Component reference designator (e.g., "U1")
            new_x: New X position in mm
            new_y: New Y position in mm

        Returns:
            DRCDelta describing the changes in violations
        """
        start_time = time.perf_counter()

        # Ensure we have state
        if self.state is None:
            self.full_check()
        assert self.state is not None

        # Get current bounds
        old_bounds = self.state.component_bounds.get(ref)
        if old_bounds is None:
            # Component not found
            return DRCDelta(check_time_ms=(time.perf_counter() - start_time) * 1000)

        # Calculate new bounds
        dx = new_x - old_bounds.center_x
        dy = new_y - old_bounds.center_y
        new_bounds = old_bounds.translate(dx, dy)

        # Find affected area (union of old and new positions, expanded by clearance)
        affected_area = old_bounds.union(new_bounds).expand(self._max_clearance)

        # Find components in affected area
        nearby_refs = self.state.spatial_index.query(affected_area)
        if ref not in nearby_refs:
            nearby_refs.append(ref)

        # Get connected nets
        connected_nets = self._component_nets.get(ref, [])

        # Check clearances for the moved component at new position
        new_violations = self._check_component_clearances(ref, new_bounds, nearby_refs)

        # Find violations that would be resolved
        resolved = [v for v in self.state.violations if v.involves(ref) and v not in new_violations]

        elapsed_ms = (time.perf_counter() - start_time) * 1000

        return DRCDelta(
            new_violations=new_violations,
            resolved_violations=resolved,
            affected_components=[ref] + [r for r in nearby_refs if r != ref],
            affected_nets=connected_nets,
            check_time_ms=elapsed_ms,
        )

    def apply_move(self, ref: str, new_x: float, new_y: float) -> DRCDelta:
        """Apply move and update cached state.

        Performs the same check as check_move() but also updates the
        cached state to reflect the new position.

        Args:
            ref: Component reference designator
            new_x: New X position in mm
            new_y: New Y position in mm

        Returns:
            DRCDelta describing the changes in violations
        """
        delta = self.check_move(ref, new_x, new_y)

        if self.state is None:
            return delta

        # Update state
        old_bounds = self.state.component_bounds.get(ref)
        if old_bounds is None:
            return delta

        # Calculate new bounds
        dx = new_x - old_bounds.center_x
        dy = new_y - old_bounds.center_y
        new_bounds = old_bounds.translate(dx, dy)

        # Update violations
        self.state.violations = [
            v for v in self.state.violations if v not in delta.resolved_violations
        ] + delta.new_violations

        # Update bounds
        self.state.component_bounds[ref] = new_bounds
        self.state.spatial_index.update(ref, new_bounds)

        return delta

    def get_current_violations(self) -> list[Violation]:
        """Get the current list of DRC violations.

        Returns:
            List of current violations, empty if not initialized
        """
        if self.state is None:
            return []
        return list(self.state.violations)

    def _build_spatial_index(self) -> None:
        """Build spatial index from PCB footprints."""
        assert self.state is not None

        for fp in self.pcb.footprints:
            bounds = self._compute_footprint_bounds(fp)
            self.state.spatial_index.insert(fp.reference, bounds)
            self.state.component_bounds[fp.reference] = bounds

    def _compute_footprint_bounds(self, fp: Footprint) -> Rectangle:
        """Compute bounding box for a footprint including all pads."""
        if not fp.pads:
            # Use footprint position with small default size
            return Rectangle.from_center(fp.position[0], fp.position[1], 1.0, 1.0)

        # Get bounds from all pads (in board coordinates)
        min_x = float("inf")
        min_y = float("inf")
        max_x = float("-inf")
        max_y = float("-inf")

        cos_a = math.cos(math.radians(fp.rotation))
        sin_a = math.sin(math.radians(fp.rotation))

        for pad in fp.pads:
            # Transform pad position to board coordinates
            local_x, local_y = pad.position
            rotated_x = local_x * cos_a - local_y * sin_a
            rotated_y = local_x * sin_a + local_y * cos_a
            abs_x = fp.position[0] + rotated_x
            abs_y = fp.position[1] + rotated_y

            # Expand by pad size
            pad_half_w = pad.size[0] / 2
            pad_half_h = pad.size[1] / 2

            min_x = min(min_x, abs_x - pad_half_w)
            min_y = min(min_y, abs_y - pad_half_h)
            max_x = max(max_x, abs_x + pad_half_w)
            max_y = max(max_y, abs_y + pad_half_h)

        return Rectangle(min_x, min_y, max_x, max_y)

    def _build_net_mapping(self) -> None:
        """Build mapping from components to their connected nets."""
        self._component_nets = {}

        for fp in self.pcb.footprints:
            nets = set()
            for pad in fp.pads:
                if pad.net_name:
                    nets.add(pad.net_name)
            self._component_nets[fp.reference] = list(nets)

    def _extract_net_segments(self) -> None:
        """Extract trace segments grouped by net."""
        assert self.state is not None

        self.state.net_segments = {}

        for seg in self.pcb.segments:
            net = self.pcb.get_net(seg.net_number)
            if net:
                if net.name not in self.state.net_segments:
                    self.state.net_segments[net.name] = []
                self.state.net_segments[net.name].append(
                    (seg.start[0], seg.start[1], seg.end[0], seg.end[1])
                )

    def _check_all(self) -> list[Violation]:
        """Run all DRC checks on the board."""
        violations: list[Violation] = []

        # Check clearances between all components
        violations.extend(self._check_all_clearances())

        return violations

    def _check_all_clearances(self) -> list[Violation]:
        """Check clearances between all components."""
        violations: list[Violation] = []

        # Get all footprint references
        refs = list(self.state.component_bounds.keys()) if self.state else []

        # Check each pair (using spatial index for efficiency)
        checked_pairs: set[tuple[str, str]] = set()

        for ref in refs:
            bounds = self.state.component_bounds.get(ref) if self.state else None
            if bounds is None:
                continue

            # Expand bounds by clearance to find nearby components
            query_bounds = bounds.expand(self._max_clearance)
            nearby = self.state.spatial_index.query(query_bounds) if self.state else []

            for other_ref in nearby:
                if other_ref == ref:
                    continue

                # Avoid checking same pair twice
                pair = tuple(sorted([ref, other_ref]))
                if pair in checked_pairs:
                    continue
                checked_pairs.add(pair)

                # Check clearance between these two components
                violation = self._check_pair_clearance(ref, other_ref)
                if violation:
                    violations.append(violation)

        return violations

    def _check_pair_clearance(self, ref1: str, ref2: str) -> Violation | None:
        """Check clearance between two components."""
        fp1 = self.pcb.get_footprint(ref1)
        fp2 = self.pcb.get_footprint(ref2)

        if fp1 is None or fp2 is None:
            return None

        # Check pad-to-pad clearances
        min_clearance = float("inf")
        min_location = (0.0, 0.0)
        min_items: tuple[str, ...] = ()
        min_nets: tuple[str, ...] = ()

        cos1 = math.cos(math.radians(fp1.rotation))
        sin1 = math.sin(math.radians(fp1.rotation))
        cos2 = math.cos(math.radians(fp2.rotation))
        sin2 = math.sin(math.radians(fp2.rotation))

        for pad1 in fp1.pads:
            # Transform pad1 to board coordinates
            local_x1, local_y1 = pad1.position
            rotated_x1 = local_x1 * cos1 - local_y1 * sin1
            rotated_y1 = local_x1 * sin1 + local_y1 * cos1
            abs_x1 = fp1.position[0] + rotated_x1
            abs_y1 = fp1.position[1] + rotated_y1
            r1 = max(pad1.size[0], pad1.size[1]) / 2

            for pad2 in fp2.pads:
                # Skip if same net (same net pads can touch)
                if pad1.net_number == pad2.net_number and pad1.net_number != 0:
                    continue

                # Transform pad2 to board coordinates
                local_x2, local_y2 = pad2.position
                rotated_x2 = local_x2 * cos2 - local_y2 * sin2
                rotated_y2 = local_x2 * sin2 + local_y2 * cos2
                abs_x2 = fp2.position[0] + rotated_x2
                abs_y2 = fp2.position[1] + rotated_y2
                r2 = max(pad2.size[0], pad2.size[1]) / 2

                # Calculate clearance (edge to edge)
                dist = math.sqrt((abs_x2 - abs_x1) ** 2 + (abs_y2 - abs_y1) ** 2)
                clearance = dist - r1 - r2

                if clearance < min_clearance:
                    min_clearance = clearance
                    min_location = ((abs_x1 + abs_x2) / 2, (abs_y1 + abs_y2) / 2)
                    min_items = (f"{ref1}-{pad1.number}", f"{ref2}-{pad2.number}")
                    min_nets = (pad1.net_name, pad2.net_name)

        # Check if below minimum clearance
        if min_clearance < self.rules.min_clearance_mm:
            return Violation(
                rule_id="clearance",
                message=f"Clearance {min_clearance:.3f}mm < minimum {self.rules.min_clearance_mm:.3f}mm",
                severity="error",
                location=min_location,
                items=min_items,
                nets=min_nets,
                actual_value=min_clearance,
                required_value=self.rules.min_clearance_mm,
            )

        return None

    def _check_component_clearances(
        self, ref: str, bounds: Rectangle, nearby_refs: list[str]
    ) -> list[Violation]:
        """Check clearances for a single component against nearby components."""
        violations: list[Violation] = []

        fp = self.pcb.get_footprint(ref)
        if fp is None:
            return violations

        # Calculate the offset from original position
        original_bounds = self.state.component_bounds.get(ref) if self.state else None
        if original_bounds is None:
            return violations

        dx = bounds.center_x - original_bounds.center_x
        dy = bounds.center_y - original_bounds.center_y

        # Create a modified position tuple
        new_position = (fp.position[0] + dx, fp.position[1] + dy)

        cos1 = math.cos(math.radians(fp.rotation))
        sin1 = math.sin(math.radians(fp.rotation))

        for other_ref in nearby_refs:
            if other_ref == ref:
                continue

            fp2 = self.pcb.get_footprint(other_ref)
            if fp2 is None:
                continue

            cos2 = math.cos(math.radians(fp2.rotation))
            sin2 = math.sin(math.radians(fp2.rotation))

            # Check pad clearances
            min_clearance = float("inf")
            min_location = (0.0, 0.0)
            min_items: tuple[str, ...] = ()
            min_nets: tuple[str, ...] = ()

            for pad1 in fp.pads:
                # Transform pad1 using NEW position
                local_x1, local_y1 = pad1.position
                rotated_x1 = local_x1 * cos1 - local_y1 * sin1
                rotated_y1 = local_x1 * sin1 + local_y1 * cos1
                abs_x1 = new_position[0] + rotated_x1
                abs_y1 = new_position[1] + rotated_y1
                r1 = max(pad1.size[0], pad1.size[1]) / 2

                for pad2 in fp2.pads:
                    # Skip if same net
                    if pad1.net_number == pad2.net_number and pad1.net_number != 0:
                        continue

                    # Transform pad2 using original position
                    local_x2, local_y2 = pad2.position
                    rotated_x2 = local_x2 * cos2 - local_y2 * sin2
                    rotated_y2 = local_x2 * sin2 + local_y2 * cos2
                    abs_x2 = fp2.position[0] + rotated_x2
                    abs_y2 = fp2.position[1] + rotated_y2
                    r2 = max(pad2.size[0], pad2.size[1]) / 2

                    dist = math.sqrt((abs_x2 - abs_x1) ** 2 + (abs_y2 - abs_y1) ** 2)
                    clearance = dist - r1 - r2

                    if clearance < min_clearance:
                        min_clearance = clearance
                        min_location = ((abs_x1 + abs_x2) / 2, (abs_y1 + abs_y2) / 2)
                        min_items = (f"{ref}-{pad1.number}", f"{other_ref}-{pad2.number}")
                        min_nets = (pad1.net_name, pad2.net_name)

            if min_clearance < self.rules.min_clearance_mm:
                violations.append(
                    Violation(
                        rule_id="clearance",
                        message=f"Clearance {min_clearance:.3f}mm < minimum {self.rules.min_clearance_mm:.3f}mm",
                        severity="error",
                        location=min_location,
                        items=min_items,
                        nets=min_nets,
                        actual_value=min_clearance,
                        required_value=self.rules.min_clearance_mm,
                    )
                )

        return violations

    def _get_connected_nets(self, ref: str) -> list[str]:
        """Get list of nets connected to a component."""
        return self._component_nets.get(ref, [])


__all__ = [
    "DRCDelta",
    "DRCState",
    "IncrementalDRC",
    "Rectangle",
    "SpatialIndex",
    "Violation",
]
