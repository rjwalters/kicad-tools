"""Net connectivity validation for PCB designs.

This module provides validation to ensure all schematic net connections
are physically routed on the PCB. It detects unrouted segments and
partially connected nets (islands).

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.validate import ConnectivityValidator
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> validator = ConnectivityValidator(pcb)
    >>> result = validator.validate()
    >>>
    >>> if result.has_issues:
    ...     for issue in result.issues:
    ...         print(f"{issue.severity}: {issue.message}")
    ...         print(f"  Fix: {issue.suggestion}")
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass(frozen=True)
class ConnectivityIssue:
    """Represents a single net connectivity issue.

    Attributes:
        severity: Either "error" or "warning"
        issue_type: Type of issue (unrouted, partial, isolated)
        net_name: Name of the affected net
        message: Human-readable description of the issue
        suggestion: Actionable fix suggestion
        connected_pads: List of connected pads (e.g., ["U1.3", "C1.1"])
        unconnected_pads: List of unconnected pads
        islands: Groups of connected pads (for partial connections)
    """

    severity: str
    issue_type: str
    net_name: str
    message: str
    suggestion: str
    connected_pads: tuple[str, ...] = ()
    unconnected_pads: tuple[str, ...] = ()
    islands: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        """Validate severity and issue_type values."""
        if self.severity not in ("error", "warning"):
            raise ValueError(f"severity must be 'error' or 'warning', got {self.severity!r}")
        valid_types = ("unrouted", "partial", "isolated")
        if self.issue_type not in valid_types:
            raise ValueError(f"issue_type must be one of {valid_types}, got {self.issue_type!r}")

    @property
    def is_error(self) -> bool:
        """Check if this is an error (not a warning)."""
        return self.severity == "error"

    @property
    def is_warning(self) -> bool:
        """Check if this is a warning (not an error)."""
        return self.severity == "warning"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "severity": self.severity,
            "issue_type": self.issue_type,
            "net_name": self.net_name,
            "message": self.message,
            "suggestion": self.suggestion,
            "connected_pads": list(self.connected_pads),
            "unconnected_pads": list(self.unconnected_pads),
            "islands": [list(island) for island in self.islands],
        }


@dataclass
class ConnectivityResult:
    """Aggregates all net connectivity issues.

    Provides convenient access to issue counts and filtering.

    Attributes:
        issues: List of all connectivity issues found
        total_nets: Total number of nets analyzed
        connected_nets: Number of fully connected nets
    """

    issues: list[ConnectivityIssue] = field(default_factory=list)
    total_nets: int = 0
    connected_nets: int = 0

    @property
    def has_issues(self) -> bool:
        """True if any issues were found."""
        return len(self.issues) > 0

    @property
    def is_fully_routed(self) -> bool:
        """True if no errors (warnings are allowed)."""
        return self.error_count == 0

    @property
    def error_count(self) -> int:
        """Count of issues with severity='error'."""
        return sum(1 for i in self.issues if i.is_error)

    @property
    def warning_count(self) -> int:
        """Count of issues with severity='warning'."""
        return sum(1 for i in self.issues if i.is_warning)

    @property
    def errors(self) -> list[ConnectivityIssue]:
        """List of only error issues."""
        return [i for i in self.issues if i.is_error]

    @property
    def warnings(self) -> list[ConnectivityIssue]:
        """List of only warning issues."""
        return [i for i in self.issues if i.is_warning]

    @property
    def unrouted(self) -> list[ConnectivityIssue]:
        """Issues with completely unrouted segments."""
        return [i for i in self.issues if i.issue_type == "unrouted"]

    @property
    def partial(self) -> list[ConnectivityIssue]:
        """Issues with partially connected nets (islands)."""
        return [i for i in self.issues if i.issue_type == "partial"]

    @property
    def isolated(self) -> list[ConnectivityIssue]:
        """Issues with isolated pads."""
        return [i for i in self.issues if i.issue_type == "isolated"]

    @property
    def unconnected_pad_count(self) -> int:
        """Total number of unconnected pads."""
        return sum(len(i.unconnected_pads) for i in self.issues)

    def __iter__(self):
        """Iterate over all issues."""
        return iter(self.issues)

    def __len__(self) -> int:
        """Total number of issues."""
        return len(self.issues)

    def __bool__(self) -> bool:
        """True if there are any issues."""
        return len(self.issues) > 0

    def add(self, issue: ConnectivityIssue) -> None:
        """Add an issue to the results."""
        self.issues.append(issue)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "is_fully_routed": self.is_fully_routed,
            "total_nets": self.total_nets,
            "connected_nets": self.connected_nets,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "unconnected_pads": self.unconnected_pad_count,
            "issues": [i.to_dict() for i in self.issues],
        }

    def summary(self) -> str:
        """Generate a human-readable summary."""
        status = "FULLY ROUTED" if self.is_fully_routed else "CONNECTIVITY ISSUES"
        parts = [
            f"Net Connectivity {status}: {self.error_count} errors, {self.warning_count} warnings"
        ]
        parts.append(f"  Nets: {self.connected_nets}/{self.total_nets} fully connected")

        if self.unrouted:
            parts.append(f"  Unrouted nets: {len(self.unrouted)}")
        if self.partial:
            parts.append(f"  Partial connections: {len(self.partial)}")
        if self.isolated:
            parts.append(f"  Isolated pads: {len(self.isolated)}")
        parts.append(f"  Total unconnected pads: {self.unconnected_pad_count}")

        return "\n".join(parts)


class ConnectivityValidator:
    """Validates net connectivity on PCB.

    Checks for:
    - Completely unrouted net segments
    - Partially connected nets (islands)
    - Isolated pads

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.validate import ConnectivityValidator
        >>>
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> validator = ConnectivityValidator(pcb)
        >>> result = validator.validate()
        >>>
        >>> if not result.is_fully_routed:
        ...     for issue in result.errors:
        ...         print(f"{issue.net_name}: {issue.message}")

    Attributes:
        pcb: Loaded PCB object
    """

    # Tolerance for matching point positions (in mm)
    POSITION_TOLERANCE = 0.001

    def __init__(self, pcb: str | Path | PCB) -> None:
        """Initialize the validator.

        Args:
            pcb: Path to PCB file or PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass

        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

    def validate(self) -> ConnectivityResult:
        """Run connectivity validation on all nets.

        Returns:
            ConnectivityResult containing all issues found
        """
        result = ConnectivityResult()

        # Get all non-empty nets (skip net 0 which is unconnected)
        nets = {n: net for n, net in self.pcb.nets.items() if n != 0 and net.name}

        result.total_nets = len(nets)
        connected_count = 0

        for net_number, net in nets.items():
            # Get all pads on this net
            pads = self._get_net_pads(net_number)

            if len(pads) < 2:
                # Single-pad nets are always "connected"
                connected_count += 1
                continue

            # Build connectivity graph from copper (segments, vias, zones)
            graph = self._build_connectivity_graph(net_number)

            # Check if all pads are connected
            islands = self._find_islands(graph, pads)

            if len(islands) <= 1:
                connected_count += 1
                continue

            # Create issue for this net
            issue = self._create_issue(net.name, pads, islands)
            result.add(issue)

        result.connected_nets = connected_count
        return result

    def _get_net_pads(self, net_number: int) -> list[str]:
        """Get all pads on a specific net.

        Args:
            net_number: Net number to find pads for

        Returns:
            List of pad identifiers in format "REF.PAD" (e.g., "U1.3")
        """
        pads = []
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            for pad in fp.pads:
                if pad.net_number == net_number:
                    pads.append(f"{fp.reference}.{pad.number}")
        return sorted(pads)

    def _build_connectivity_graph(
        self,
        net_number: int,
    ) -> dict[str, set[str]]:
        """Build graph of copper connectivity for a net.

        Creates a graph where nodes are points (pad positions, track endpoints,
        via positions) and edges connect points that are electrically connected.

        Args:
            net_number: Net number to analyze

        Returns:
            Adjacency list mapping point IDs to connected point IDs
        """
        graph: dict[str, set[str]] = defaultdict(set)

        # Get all pad positions for this net
        pad_positions: dict[str, tuple[float, float]] = {}
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue
            # Get footprint position and rotation for pad position calculation
            fp_x, fp_y = fp.position
            rotation = fp.rotation

            for pad in fp.pads:
                if pad.net_number == net_number:
                    pad_id = f"{fp.reference}.{pad.number}"
                    # Transform pad position from footprint-local to board coordinates
                    pad_x, pad_y = self._transform_pad_position(pad.position, fp_x, fp_y, rotation)
                    pad_positions[pad_id] = (pad_x, pad_y)

        # Get all track segment endpoints for this net
        segments = list(self.pcb.segments_in_net(net_number))
        segment_points: list[tuple[float, float]] = []
        for seg in segments:
            segment_points.append(seg.start)
            segment_points.append(seg.end)

        # Get all via positions for this net
        vias = list(self.pcb.vias_in_net(net_number))
        via_positions = [via.position for via in vias]

        # Check zones for filled polygons on this net
        zone_points: list[tuple[float, float]] = []
        for zone in self.pcb.zones:
            if zone.net_number == net_number and zone.filled_polygons:
                # Sample points from filled polygons
                for poly in zone.filled_polygons:
                    zone_points.extend(poly)

        # All copper points
        all_copper_points = segment_points + via_positions + zone_points

        # Connect pads that are at the same location as copper
        for pad_id, pad_pos in pad_positions.items():
            for copper_pos in all_copper_points:
                if self._points_close(pad_pos, copper_pos):
                    # Find other pads at this copper point
                    for other_id, other_pos in pad_positions.items():
                        if other_id != pad_id and self._points_close(pad_pos, other_pos):
                            graph[pad_id].add(other_id)
                            graph[other_id].add(pad_id)

        # Connect pads through track segments
        for seg in segments:
            # Find pads at segment endpoints
            start_pads = self._find_pads_at_point(seg.start, pad_positions)
            end_pads = self._find_pads_at_point(seg.end, pad_positions)

            # Connect pads at start to pads at end
            for start_pad in start_pads:
                for end_pad in end_pads:
                    if start_pad != end_pad:
                        graph[start_pad].add(end_pad)
                        graph[end_pad].add(start_pad)

            # Also connect pads at each endpoint to themselves (for via chains)
            for pad in start_pads:
                for other in start_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

            for pad in end_pads:
                for other in end_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Connect pads through via chains
        for via in vias:
            via_pads = self._find_pads_at_point(via.position, pad_positions)
            for pad in via_pads:
                for other in via_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Build full transitive closure through segment chains
        # Track endpoints can form chains connecting distant pads
        graph = self._build_segment_chains(segments, pad_positions, graph)

        return graph

    def _build_segment_chains(
        self,
        segments: list,
        pad_positions: dict[str, tuple[float, float]],
        graph: dict[str, set[str]],
    ) -> dict[str, set[str]]:
        """Build connectivity through chains of connected segments.

        Segments that share endpoints form chains. Pads at any point
        in a chain are connected to all other pads in the chain.
        """
        if not segments:
            return graph

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        for i, seg_a in enumerate(segments):
            for j, seg_b in enumerate(segments):
                if i != j:
                    # Check if segments share an endpoint
                    if (
                        self._points_close(seg_a.start, seg_b.start)
                        or self._points_close(seg_a.start, seg_b.end)
                        or self._points_close(seg_a.end, seg_b.start)
                        or self._points_close(seg_a.end, seg_b.end)
                    ):
                        segment_graph[i].add(j)
                        segment_graph[j].add(i)

        # Find connected components of segments
        visited: set[int] = set()
        components: list[set[int]] = []

        for i in range(len(segments)):
            if i in visited:
                continue
            component: set[int] = set()
            queue = [i]
            while queue:
                seg_idx = queue.pop()
                if seg_idx in visited:
                    continue
                visited.add(seg_idx)
                component.add(seg_idx)
                queue.extend(segment_graph[seg_idx] - visited)
            components.append(component)

        # For each component, find all pads and connect them
        for component in components:
            component_pads: set[str] = set()
            for seg_idx in component:
                seg = segments[seg_idx]
                component_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                component_pads.update(self._find_pads_at_point(seg.end, pad_positions))

            # Connect all pads in this component
            pad_list = list(component_pads)
            for i, pad in enumerate(pad_list):
                for other in pad_list[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

        return graph

    def _transform_pad_position(
        self,
        pad_local: tuple[float, float],
        fp_x: float,
        fp_y: float,
        rotation: float,
    ) -> tuple[float, float]:
        """Transform pad position from footprint-local to board coordinates.

        Args:
            pad_local: Pad position in footprint-local coordinates
            fp_x: Footprint X position
            fp_y: Footprint Y position
            rotation: Footprint rotation in degrees

        Returns:
            Pad position in board coordinates
        """
        import math

        # Convert rotation to radians
        angle = math.radians(rotation)

        # Rotate pad position
        px, py = pad_local
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        rotated_x = px * cos_a - py * sin_a
        rotated_y = px * sin_a + py * cos_a

        # Translate to board coordinates
        board_x = fp_x + rotated_x
        board_y = fp_y + rotated_y

        return (board_x, board_y)

    def _find_pads_at_point(
        self,
        point: tuple[float, float],
        pad_positions: dict[str, tuple[float, float]],
    ) -> list[str]:
        """Find all pads at a given point.

        Args:
            point: Point to check
            pad_positions: Mapping of pad IDs to positions

        Returns:
            List of pad IDs at this point
        """
        return [
            pad_id
            for pad_id, pad_pos in pad_positions.items()
            if self._points_close(point, pad_pos)
        ]

    def _points_close(
        self,
        p1: tuple[float, float],
        p2: tuple[float, float],
    ) -> bool:
        """Check if two points are within tolerance distance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (self.POSITION_TOLERANCE * self.POSITION_TOLERANCE)

    def _find_islands(
        self,
        graph: dict[str, set[str]],
        pads: list[str],
    ) -> list[list[str]]:
        """Find disconnected islands in connectivity graph.

        Uses BFS to find connected components among the given pads.

        Args:
            graph: Adjacency list of pad connectivity
            pads: List of pad IDs to check

        Returns:
            List of islands, each island is a list of connected pads
        """
        visited: set[str] = set()
        islands: list[list[str]] = []

        for pad in pads:
            if pad in visited:
                continue

            # BFS to find all connected pads
            island: list[str] = []
            queue = [pad]

            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)

                # Only include pads from our list
                if current in pads:
                    island.append(current)

                # Add neighbors
                for neighbor in graph.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

            if island:
                islands.append(sorted(island))

        return islands

    def _create_issue(
        self,
        net_name: str,
        pads: list[str],
        islands: list[list[str]],
    ) -> ConnectivityIssue:
        """Create a connectivity issue for a net with multiple islands.

        Args:
            net_name: Name of the net
            pads: All pads on this net
            islands: List of disconnected islands

        Returns:
            ConnectivityIssue describing the problem
        """
        # Sort islands by size (largest first)
        islands = sorted(islands, key=len, reverse=True)

        # Largest island is "connected", rest are "unconnected"
        connected = islands[0] if islands else []
        unconnected_islands = islands[1:] if len(islands) > 1 else []
        unconnected = []
        for island in unconnected_islands:
            unconnected.extend(island)

        if len(islands) == 2:
            # Two islands - partial connection
            issue_type = "partial"
            message = f"Net '{net_name}' has 2 disconnected islands"
            suggestion = (
                f"Connect islands (missing trace between {islands[0][-1]} and {islands[1][0]})"
            )
        else:
            # More than two islands
            issue_type = "partial"
            message = f"Net '{net_name}' has {len(islands)} disconnected islands"
            suggestion = f"Connect {len(islands)} islands to complete routing"

        return ConnectivityIssue(
            severity="error",
            issue_type=issue_type,
            net_name=net_name,
            message=message,
            suggestion=suggestion,
            connected_pads=tuple(connected),
            unconnected_pads=tuple(unconnected),
            islands=tuple(tuple(island) for island in islands),
        )

    def __repr__(self) -> str:
        """Return string representation."""
        net_count = self.pcb.net_count if self.pcb else 0
        return f"ConnectivityValidator(nets={net_count})"
