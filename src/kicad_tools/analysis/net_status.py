"""Net connectivity status analysis for PCB designs.

This module provides detailed net connectivity status reporting, showing which nets
are complete, incomplete, or unrouted, with details on what's missing.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis.net_status import NetStatusAnalyzer
    >>>
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = NetStatusAnalyzer(pcb)
    >>> result = analyzer.analyze()
    >>>
    >>> for net_status in result.incomplete:
    ...     print(f"{net_status.net_name}: {net_status.unconnected_count} unconnected")
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass
class PadInfo:
    """Information about a pad on a net."""

    reference: str  # Component reference (e.g., "U1")
    pad_number: str  # Pad number (e.g., "2")
    position: tuple[float, float]  # Board coordinates (x, y)
    is_connected: bool  # Whether pad is connected to main routing
    layers: list[str] = field(default_factory=list)  # Layers pad exists on

    @property
    def full_name(self) -> str:
        """Full pad name as REF.PAD (e.g., 'C2.2')."""
        return f"{self.reference}.{self.pad_number}"


@dataclass
class NetStatus:
    """Status of a single net.

    Attributes:
        net_number: Net number in PCB
        net_name: Net name (e.g., "GND", "+3.3V")
        net_class: Net class if assigned
        total_pads: Total number of pads on this net
        connected_pads: List of connected pads
        unconnected_pads: List of unconnected pads
        is_plane_net: Whether this net is connected to a copper zone
        plane_layer: Layer of the copper zone if is_plane_net
        has_routing: Whether this net has any trace segments
        has_vias: Whether this net has any vias
    """

    net_number: int
    net_name: str
    net_class: str = ""
    total_pads: int = 0
    connected_pads: list[PadInfo] = field(default_factory=list)
    unconnected_pads: list[PadInfo] = field(default_factory=list)
    is_plane_net: bool = False
    plane_layer: str = ""
    has_routing: bool = False
    has_vias: bool = False

    @property
    def connected_count(self) -> int:
        """Number of connected pads."""
        return len(self.connected_pads)

    @property
    def unconnected_count(self) -> int:
        """Number of unconnected pads."""
        return len(self.unconnected_pads)

    @property
    def connection_percentage(self) -> float:
        """Percentage of pads connected (0-100)."""
        if self.total_pads == 0:
            return 100.0
        return (self.connected_count / self.total_pads) * 100

    @property
    def status(self) -> str:
        """Net status: 'complete', 'incomplete', or 'unrouted'."""
        if self.total_pads <= 1:
            return "complete"
        if self.unconnected_count == 0:
            return "complete"
        if self.connected_count == 0:
            return "unrouted"
        return "incomplete"

    @property
    def net_type(self) -> str:
        """Net type: 'plane', 'signal', or 'power'."""
        if self.is_plane_net:
            return "plane"
        # Common power net patterns
        if self.net_name.startswith(("+", "-", "V")) or self.net_name in (
            "GND",
            "AGND",
            "DGND",
            "VCC",
            "VDD",
            "VSS",
        ):
            return "power"
        return "signal"

    @property
    def suggested_fix(self) -> str:
        """Suggest fix based on net type."""
        if self.is_plane_net:
            return f"kicad-pcb-stitch board.kicad_pcb --net {self.net_name}"
        return f"Route traces to connect {self.unconnected_count} pads"

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "net_number": self.net_number,
            "net_name": self.net_name,
            "net_class": self.net_class,
            "status": self.status,
            "net_type": self.net_type,
            "total_pads": self.total_pads,
            "connected_count": self.connected_count,
            "unconnected_count": self.unconnected_count,
            "connection_percentage": round(self.connection_percentage, 1),
            "is_plane_net": self.is_plane_net,
            "plane_layer": self.plane_layer,
            "has_routing": self.has_routing,
            "has_vias": self.has_vias,
            "suggested_fix": self.suggested_fix if self.status != "complete" else "",
            "connected_pads": [
                {
                    "name": p.full_name,
                    "position": list(p.position),
                }
                for p in self.connected_pads
            ],
            "unconnected_pads": [
                {
                    "name": p.full_name,
                    "position": list(p.position),
                }
                for p in self.unconnected_pads
            ],
        }


@dataclass
class NetStatusResult:
    """Aggregates net status for all nets in a PCB.

    Attributes:
        nets: List of all analyzed net statuses
        total_nets: Total number of nets analyzed
    """

    nets: list[NetStatus] = field(default_factory=list)
    total_nets: int = 0

    @property
    def complete(self) -> list[NetStatus]:
        """Nets that are fully connected."""
        return [n for n in self.nets if n.status == "complete"]

    @property
    def incomplete(self) -> list[NetStatus]:
        """Nets that are partially connected."""
        return [n for n in self.nets if n.status == "incomplete"]

    @property
    def unrouted(self) -> list[NetStatus]:
        """Nets with no routing at all."""
        return [n for n in self.nets if n.status == "unrouted"]

    @property
    def complete_count(self) -> int:
        """Number of complete nets."""
        return len(self.complete)

    @property
    def incomplete_count(self) -> int:
        """Number of incomplete nets."""
        return len(self.incomplete)

    @property
    def unrouted_count(self) -> int:
        """Number of unrouted nets."""
        return len(self.unrouted)

    @property
    def total_unconnected_pads(self) -> int:
        """Total number of unconnected pads across all nets."""
        return sum(n.unconnected_count for n in self.nets)

    def by_net_class(self) -> dict[str, list[NetStatus]]:
        """Group nets by net class."""
        result: dict[str, list[NetStatus]] = defaultdict(list)
        for net in self.nets:
            class_name = net.net_class or "Default"
            result[class_name].append(net)
        return dict(result)

    def get_net(self, net_name: str) -> NetStatus | None:
        """Get status for a specific net by name."""
        for net in self.nets:
            if net.net_name == net_name:
                return net
        return None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_nets": self.total_nets,
            "complete_count": self.complete_count,
            "incomplete_count": self.incomplete_count,
            "unrouted_count": self.unrouted_count,
            "total_unconnected_pads": self.total_unconnected_pads,
            "nets": [n.to_dict() for n in self.nets],
        }

    def summary(self) -> str:
        """Generate human-readable summary."""
        lines = [
            f"Net Status Summary: {self.total_nets} nets total",
            f"  Complete:   {self.complete_count} (100% connected)",
            f"  Incomplete: {self.incomplete_count} (partially connected)",
            f"  Unrouted:   {self.unrouted_count} (0% connected)",
            f"  Total unconnected pads: {self.total_unconnected_pads}",
        ]
        return "\n".join(lines)


class NetStatusAnalyzer:
    """Analyzes net connectivity status on a PCB.

    Provides detailed status for each net including:
    - Complete/incomplete/unrouted classification
    - Identification of plane nets vs signal nets
    - Location of unconnected pads with coordinates
    - Suggested fixes

    Example:
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> analyzer = NetStatusAnalyzer(pcb)
        >>> result = analyzer.analyze()
        >>> print(result.summary())
    """

    # Tolerance for matching point positions (in mm)
    POSITION_TOLERANCE = 0.01

    def __init__(self, pcb: str | Path | PCB) -> None:
        """Initialize the analyzer.

        Args:
            pcb: Path to PCB file or loaded PCB object
        """
        from kicad_tools.schema.pcb import PCB as PCBClass

        if isinstance(pcb, (str, Path)):
            self.pcb = PCBClass.load(str(pcb))
        else:
            self.pcb = pcb

    def analyze(self) -> NetStatusResult:
        """Analyze all nets and return status result.

        Returns:
            NetStatusResult containing status for all nets
        """
        result = NetStatusResult()

        # Get all non-empty nets (skip net 0 which is unconnected)
        nets = {n: net for n, net in self.pcb.nets.items() if n != 0 and net.name}
        result.total_nets = len(nets)

        # Build zone lookup for plane net detection
        zone_nets = self._build_zone_net_map()

        for net_number, net in nets.items():
            status = self._analyze_net(net_number, net.name, zone_nets)
            result.nets.append(status)

        # Sort by status (incomplete first, then unrouted, then complete)
        status_order = {"incomplete": 0, "unrouted": 1, "complete": 2}
        result.nets.sort(key=lambda n: (status_order.get(n.status, 3), n.net_name))

        return result

    def _build_zone_net_map(self) -> dict[int, str]:
        """Build mapping of net numbers to zone layers.

        Returns:
            Dict mapping net_number to zone layer name
        """
        zone_nets: dict[int, str] = {}
        for zone in self.pcb.zones:
            if zone.net_number > 0:
                zone_nets[zone.net_number] = zone.layer
        return zone_nets

    def _analyze_net(
        self,
        net_number: int,
        net_name: str,
        zone_nets: dict[int, str],
    ) -> NetStatus:
        """Analyze a single net.

        Args:
            net_number: Net number
            net_name: Net name
            zone_nets: Mapping of net numbers to zone layers

        Returns:
            NetStatus for this net
        """
        status = NetStatus(
            net_number=net_number,
            net_name=net_name,
        )

        # Check if this is a plane net
        if net_number in zone_nets:
            status.is_plane_net = True
            status.plane_layer = zone_nets[net_number]

        # Check for routing
        segments = list(self.pcb.segments_in_net(net_number))
        status.has_routing = len(segments) > 0

        # Check for vias
        vias = list(self.pcb.vias_in_net(net_number))
        status.has_vias = len(vias) > 0

        # Get all pads on this net with their positions
        pad_infos = self._get_net_pads_with_positions(net_number)
        status.total_pads = len(pad_infos)

        if len(pad_infos) < 2:
            # Single-pad nets are always "complete"
            status.connected_pads = pad_infos
            return status

        # Build connectivity graph
        graph = self._build_connectivity_graph(net_number, pad_infos)

        # Find connected components (islands)
        islands = self._find_islands(graph, [p.full_name for p in pad_infos])

        # Largest island is considered "connected"
        if islands:
            islands.sort(key=len, reverse=True)
            connected_names = set(islands[0])
        else:
            connected_names = set()

        # Classify pads
        for pad_info in pad_infos:
            pad_info.is_connected = pad_info.full_name in connected_names
            if pad_info.is_connected:
                status.connected_pads.append(pad_info)
            else:
                status.unconnected_pads.append(pad_info)

        # Sort unconnected pads by position for consistent output
        status.unconnected_pads.sort(key=lambda p: (p.reference, p.pad_number))

        return status

    def _get_net_pads_with_positions(self, net_number: int) -> list[PadInfo]:
        """Get all pads on a net with their board positions.

        Args:
            net_number: Net number to find pads for

        Returns:
            List of PadInfo objects
        """
        pads = []
        for fp in self.pcb.footprints:
            if not fp.reference or fp.reference.startswith("#"):
                continue

            fp_x, fp_y = fp.position
            rotation = fp.rotation

            for pad in fp.pads:
                if pad.net_number == net_number:
                    # Transform pad position to board coordinates
                    board_pos = self._transform_pad_position(pad.position, fp_x, fp_y, rotation)
                    pads.append(
                        PadInfo(
                            reference=fp.reference,
                            pad_number=pad.number,
                            position=board_pos,
                            is_connected=False,
                            layers=pad.layers,
                        )
                    )
        return pads

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
        angle = math.radians(rotation)
        px, py = pad_local
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)

        rotated_x = px * cos_a - py * sin_a
        rotated_y = px * sin_a + py * cos_a

        return (fp_x + rotated_x, fp_y + rotated_y)

    def _build_connectivity_graph(
        self,
        net_number: int,
        pad_infos: list[PadInfo],
    ) -> dict[str, set[str]]:
        """Build connectivity graph for a net.

        Args:
            net_number: Net number
            pad_infos: List of pad info objects

        Returns:
            Adjacency list mapping pad names to connected pad names
        """
        graph: dict[str, set[str]] = defaultdict(set)
        pad_positions = {p.full_name: p.position for p in pad_infos}
        pad_layers = {p.full_name: p.layers for p in pad_infos}

        # Get segments and vias for this net
        segments = list(self.pcb.segments_in_net(net_number))
        vias = list(self.pcb.vias_in_net(net_number))

        # Get zones for this net with their layers and filled polygons
        net_zones: list[tuple[str, list[list[tuple[float, float]]]]] = []
        zone_points: list[tuple[float, float]] = []
        for zone in self.pcb.zones:
            if zone.net_number == net_number and zone.filled_polygons:
                net_zones.append((zone.layer, zone.filled_polygons))
                for poly in zone.filled_polygons:
                    zone_points.extend(poly)

        # Build segment components for reuse
        segment_components = self._build_segment_components(segments)

        # Connect pads through segment chains
        for component in segment_components:
            component_pads: set[str] = set()
            for seg_idx in component:
                seg = segments[seg_idx]
                component_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                component_pads.update(self._find_pads_at_point(seg.end, pad_positions))

            pad_list = list(component_pads)
            for i, pad in enumerate(pad_list):
                for other in pad_list[i + 1 :]:
                    graph[pad].add(other)
                    graph[other].add(pad)

        # Connect pads through vias (pads at same via position)
        for via in vias:
            via_pads = self._find_pads_at_point(via.position, pad_positions)
            for pad in via_pads:
                for other in via_pads:
                    if pad != other:
                        graph[pad].add(other)
                        graph[other].add(pad)

        # Find zone connection points (via positions that touch zones)
        zone_connection_points: set[tuple[float, float]] = set()
        for zone_layer, filled_polys in net_zones:
            for via in vias:
                if zone_layer not in via.layers:
                    continue
                for poly in filled_polys:
                    if self._point_in_polygon(via.position, poly):
                        zone_connection_points.add(via.position)
                        break

        # Connect pads through via-to-zone connectivity
        # A pad is zone-connected if:
        # 1. It's directly at a zone connection point, OR
        # 2. It's in a segment chain that touches a zone connection point
        zone_connected_pads: set[str] = set()

        # Pads directly at zone connection points
        for zcp in zone_connection_points:
            zone_connected_pads.update(self._find_pads_at_point(zcp, pad_positions))

        # Pads that directly overlap with zone filled polygons (Issue #441)
        # This handles through-hole pads that connect to inner layer zones
        for pad_id, pad_pos in pad_positions.items():
            layers = pad_layers.get(pad_id, [])
            for zone_layer, filled_polys in net_zones:
                if not self._pad_layer_matches_zone(layers, zone_layer):
                    continue
                for poly in filled_polys:
                    if self._point_in_polygon(pad_pos, poly):
                        zone_connected_pads.add(pad_id)
                        break

        # Pads connected via segment chains that touch zone connection points
        for component in segment_components:
            touches_zone = False
            for seg_idx in component:
                seg = segments[seg_idx]
                for zcp in zone_connection_points:
                    if self._points_close(seg.start, zcp) or self._points_close(seg.end, zcp):
                        touches_zone = True
                        break
                if touches_zone:
                    break

            if touches_zone:
                # All pads in this segment chain are connected to the zone
                for seg_idx in component:
                    seg = segments[seg_idx]
                    zone_connected_pads.update(self._find_pads_at_point(seg.start, pad_positions))
                    zone_connected_pads.update(self._find_pads_at_point(seg.end, pad_positions))

        # Connect all zone-connected pads to each other
        zone_pad_list = list(zone_connected_pads)
        for i, pad in enumerate(zone_pad_list):
            for other in zone_pad_list[i + 1 :]:
                graph[pad].add(other)
                graph[other].add(pad)

        # Connect pads at same copper positions (including zones)
        all_copper = [seg.start for seg in segments] + [seg.end for seg in segments]
        all_copper.extend([via.position for via in vias])
        all_copper.extend(zone_points[:1000])  # Limit zone sampling

        for pad_id, pad_pos in pad_positions.items():
            for copper_pos in all_copper:
                if self._points_close(pad_pos, copper_pos):
                    # Find other pads at this copper point
                    for other_id, other_pos in pad_positions.items():
                        if other_id != pad_id and self._points_close(copper_pos, other_pos):
                            graph[pad_id].add(other_id)
                            graph[other_id].add(pad_id)

        return graph

    def _build_segment_components(self, segments: list) -> list[set[int]]:
        """Build connected components of segments.

        Args:
            segments: List of trace segments

        Returns:
            List of sets, each containing segment indices in a connected component
        """
        if not segments:
            return []

        # Build segment adjacency graph
        segment_graph: dict[int, set[int]] = defaultdict(set)
        for i, seg_a in enumerate(segments):
            for j, seg_b in enumerate(segments):
                if i != j:
                    if (
                        self._points_close(seg_a.start, seg_b.start)
                        or self._points_close(seg_a.start, seg_b.end)
                        or self._points_close(seg_a.end, seg_b.start)
                        or self._points_close(seg_a.end, seg_b.end)
                    ):
                        segment_graph[i].add(j)
                        segment_graph[j].add(i)

        # Find connected components
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

        return components

    def _find_pads_at_point(
        self,
        point: tuple[float, float],
        pad_positions: dict[str, tuple[float, float]],
    ) -> list[str]:
        """Find pads at a given point.

        Args:
            point: Point to check
            pad_positions: Mapping of pad names to positions

        Returns:
            List of pad names at this point
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
        """Check if two points are within tolerance."""
        dx = p1[0] - p2[0]
        dy = p1[1] - p2[1]
        return (dx * dx + dy * dy) < (self.POSITION_TOLERANCE * self.POSITION_TOLERANCE)

    def _pad_layer_matches_zone(
        self,
        pad_layers: list[str],
        zone_layer: str,
    ) -> bool:
        """Check if a pad exists on the same layer as a zone.

        Handles wildcard layers like "*.Cu" which match any copper layer.

        Args:
            pad_layers: List of layers the pad exists on
            zone_layer: Layer the zone is on (e.g., "In1.Cu", "B.Cu")

        Returns:
            True if the pad and zone share a layer
        """
        for pad_layer in pad_layers:
            # Exact match
            if pad_layer == zone_layer:
                return True
            # Wildcard match: "*.Cu" matches any copper layer
            if pad_layer == "*.Cu" and zone_layer.endswith(".Cu"):
                return True
            # Also handle "*.Mask" style wildcards
            if pad_layer.startswith("*.") and zone_layer.endswith(pad_layer[1:]):
                return True
        return False

    def _point_in_polygon(
        self,
        point: tuple[float, float],
        polygon: list[tuple[float, float]],
    ) -> bool:
        """Test if point is inside polygon using ray casting algorithm.

        Args:
            point: (x, y) coordinates to test
            polygon: List of (x, y) vertices

        Returns:
            True if point is inside polygon
        """
        n = len(polygon)
        if n < 3:
            return False

        x, y = point
        inside = False
        j = n - 1

        for i in range(n):
            xi, yi = polygon[i]
            xj, yj = polygon[j]

            if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
                inside = not inside
            j = i

        return inside

    def _find_islands(
        self,
        graph: dict[str, set[str]],
        pads: list[str],
    ) -> list[list[str]]:
        """Find disconnected islands in connectivity graph.

        Args:
            graph: Adjacency list
            pads: List of pad names

        Returns:
            List of islands (connected components)
        """
        visited: set[str] = set()
        islands: list[list[str]] = []

        for pad in pads:
            if pad in visited:
                continue

            island: list[str] = []
            queue = [pad]

            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)

                if current in pads:
                    island.append(current)

                for neighbor in graph.get(current, set()):
                    if neighbor not in visited:
                        queue.append(neighbor)

            if island:
                islands.append(sorted(island))

        return islands
