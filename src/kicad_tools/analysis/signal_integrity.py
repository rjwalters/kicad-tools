"""Signal integrity analysis for PCB designs.

Analyzes signal integrity concerns including crosstalk risk between
adjacent traces and impedance discontinuities from geometry changes.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import SignalIntegrityAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = SignalIntegrityAnalyzer()
    >>> crosstalk = analyzer.analyze_crosstalk(pcb)
    >>> for risk in crosstalk:
    ...     print(f"{risk.risk_level}: {risk.aggressor_net} ↔ {risk.victim_net}")
    >>> impedance = analyzer.analyze_impedance(pcb)
    >>> for disc in impedance:
    ...     print(f"{disc.net}: {disc.mismatch_percent:.0f}% mismatch at {disc.cause}")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB, Segment


class RiskLevel(Enum):
    """Signal integrity risk level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# Pattern for identifying high-speed nets
HIGH_SPEED_NET_PATTERNS = [
    # Clock signals
    r"(?i)^CLK",
    r"(?i)CLK$",
    r"(?i)CLOCK",
    r"(?i)_CLK_",
    # USB differential pairs
    r"(?i)USB.*D[PM]$",
    r"(?i)USB.*[DP][\+\-]?$",
    r"(?i)USB.*DATA",
    r"(?i)^D[\+\-]$",
    # High-speed serial
    r"(?i)LVDS",
    r"(?i)MIPI",
    r"(?i)HDMI",
    r"(?i)DP_",  # DisplayPort
    r"(?i)PCIE",
    r"(?i)SATA",
    # DDR memory
    r"(?i)DDR",
    r"(?i)^DQ\d",
    r"(?i)^DQS",
    r"(?i)^DM\d",
    # Ethernet
    r"(?i)ETH.*[TP][\+\-]?",
    r"(?i)RGMII",
    r"(?i)RMII",
    # High-speed data
    r"(?i)MOSI",
    r"(?i)MISO",
    r"(?i)SCK",
    r"(?i)SPI.*CLK",
]


@dataclass
class CrosstalkRisk:
    """Crosstalk risk between two nets.

    Attributes:
        aggressor_net: Name of the aggressor (source) net.
        victim_net: Name of the victim (receiving) net.
        parallel_length_mm: Length of parallel trace run in mm.
        spacing_mm: Edge-to-edge spacing between traces in mm.
        layer: Layer where coupling occurs.
        coupling_coefficient: Estimated coupling coefficient (0-1).
        risk_level: Risk level classification.
        suggestion: Actionable suggestion for mitigation.
    """

    aggressor_net: str
    victim_net: str

    # Coupling info
    parallel_length_mm: float
    spacing_mm: float
    layer: str

    # Risk assessment
    coupling_coefficient: float  # 0-1
    risk_level: RiskLevel

    # Suggestion
    suggestion: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "aggressor_net": self.aggressor_net,
            "victim_net": self.victim_net,
            "parallel_length_mm": round(self.parallel_length_mm, 2),
            "spacing_mm": round(self.spacing_mm, 3),
            "layer": self.layer,
            "coupling_coefficient": round(self.coupling_coefficient, 3),
            "risk_level": self.risk_level.value,
            "suggestion": self.suggestion,
        }


@dataclass
class ImpedanceDiscontinuity:
    """Impedance mismatch in a trace.

    Attributes:
        net: Name of the net with discontinuity.
        position: (x, y) position of discontinuity in mm.
        impedance_before: Impedance before discontinuity in ohms.
        impedance_after: Impedance after discontinuity in ohms.
        mismatch_percent: Percentage difference in impedance.
        cause: Type of discontinuity (width_change, via, layer_change, stub).
        suggestion: Actionable suggestion for fixing.
    """

    net: str
    position: tuple[float, float]

    # Impedance values
    impedance_before: float  # Ohms
    impedance_after: float  # Ohms
    mismatch_percent: float

    # Cause
    cause: str  # "width_change", "via", "layer_change", "stub"

    # Suggestion
    suggestion: str

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "net": self.net,
            "position": {"x": round(self.position[0], 2), "y": round(self.position[1], 2)},
            "impedance_before_ohms": round(self.impedance_before, 1),
            "impedance_after_ohms": round(self.impedance_after, 1),
            "mismatch_percent": round(self.mismatch_percent, 1),
            "cause": self.cause,
            "suggestion": self.suggestion,
        }


@dataclass
class _TrackRun:
    """Internal representation of a contiguous track run."""

    segments: list[Segment] = field(default_factory=list)
    net_number: int = 0
    net_name: str = ""
    layer: str = ""
    start: tuple[float, float] = (0.0, 0.0)
    end: tuple[float, float] = (0.0, 0.0)
    total_length: float = 0.0

    @property
    def midpoint(self) -> tuple[float, float]:
        """Return the midpoint of the track run."""
        return (
            (self.start[0] + self.end[0]) / 2,
            (self.start[1] + self.end[1]) / 2,
        )


class SignalIntegrityAnalyzer:
    """Analyze signal integrity concerns on a PCB.

    Detects crosstalk risk between adjacent traces and impedance
    discontinuities from width changes, vias, and layer transitions.

    Args:
        min_parallel_length: Minimum parallel length (mm) to consider for crosstalk.
        max_coupling_distance: Maximum spacing (mm) to consider for coupling.
        high_speed_patterns: Additional regex patterns for high-speed net detection.
    """

    # Coupling thresholds for risk classification
    COUPLING_LOW = 0.1
    COUPLING_MEDIUM = 0.3
    COUPLING_HIGH = 0.5

    # Impedance mismatch thresholds (percentage)
    MISMATCH_WARN = 10.0  # 10% mismatch warning
    MISMATCH_ERROR = 25.0  # 25% mismatch error

    def __init__(
        self,
        min_parallel_length: float = 3.0,
        max_coupling_distance: float = 0.5,
        high_speed_patterns: list[str] | None = None,
    ):
        """Initialize the analyzer.

        Args:
            min_parallel_length: Minimum parallel length in mm to consider.
            max_coupling_distance: Maximum edge spacing in mm to consider.
            high_speed_patterns: Additional regex patterns for high-speed nets.
        """
        self.min_parallel_length = min_parallel_length
        self.max_coupling_distance = max_coupling_distance

        self._patterns = list(HIGH_SPEED_NET_PATTERNS)
        if high_speed_patterns:
            self._patterns.extend(high_speed_patterns)
        self._compiled_patterns = [re.compile(p) for p in self._patterns]

    def analyze_crosstalk(self, board: PCB) -> list[CrosstalkRisk]:
        """Find traces with crosstalk risk.

        Identifies high-speed nets and finds adjacent parallel traces
        that may experience crosstalk coupling.

        Args:
            board: PCB object to analyze.

        Returns:
            List of CrosstalkRisk objects for concerning trace pairs,
            sorted by risk level (highest first).
        """
        # Get high-speed net numbers
        high_speed_nets = self._identify_high_speed_nets(board)
        if not high_speed_nets:
            return []

        net_names = {net.number: net.name for net in board.nets.values()}
        risks: list[CrosstalkRisk] = []
        analyzed_pairs: set[tuple[int, int]] = set()

        # Build track runs for each high-speed net on each layer
        for net_number in high_speed_nets:
            net_name = net_names.get(net_number, f"Net{net_number}")
            layer_runs = self._build_track_runs(board, net_number, net_name)

            # Find adjacent tracks from other nets
            for layer, runs in layer_runs.items():
                for run in runs:
                    adjacent = self._find_adjacent_tracks(board, run, net_names)
                    for adj_run, spacing in adjacent:
                        # Skip if already analyzed this pair
                        pair_key = tuple(sorted([net_number, adj_run.net_number]))
                        if pair_key in analyzed_pairs:
                            continue
                        analyzed_pairs.add(pair_key)

                        risk = self._calculate_crosstalk_risk(
                            run, adj_run, spacing, net_name, net_names
                        )
                        if risk.risk_level != RiskLevel.LOW:
                            risks.append(risk)

        # Sort by risk level (high first)
        risk_order = {RiskLevel.HIGH: 0, RiskLevel.MEDIUM: 1, RiskLevel.LOW: 2}
        risks.sort(key=lambda r: (risk_order[r.risk_level], -r.coupling_coefficient))

        return risks

    def analyze_impedance(self, board: PCB) -> list[ImpedanceDiscontinuity]:
        """Find impedance discontinuities.

        Detects width changes, vias, and layer changes that may cause
        impedance mismatches in controlled-impedance traces.

        Args:
            board: PCB object to analyze.

        Returns:
            List of ImpedanceDiscontinuity objects for each issue found,
            sorted by severity.
        """
        discontinuities: list[ImpedanceDiscontinuity] = []
        net_names = {net.number: net.name for net in board.nets.values()}

        # Focus on high-speed nets that are likely controlled impedance
        high_speed_nets = self._identify_high_speed_nets(board)

        for net_number in high_speed_nets:
            net_name = net_names.get(net_number, f"Net{net_number}")

            # Get all segments for this net
            segments = list(board.segments_in_net(net_number))
            if len(segments) < 2:
                continue

            # Sort segments by position to find connected pairs
            # (This is a simplification - real connectivity is complex)
            segments_sorted = sorted(segments, key=lambda s: (s.start[0], s.start[1]))

            # Check for width changes between adjacent segments on same layer
            for i, seg in enumerate(segments_sorted[:-1]):
                next_seg = segments_sorted[i + 1]

                # Only check segments on same layer
                if seg.layer != next_seg.layer:
                    continue

                # Check if segments are connected (share an endpoint)
                if not self._segments_connected(seg, next_seg):
                    continue

                # Check for width change
                if abs(seg.width - next_seg.width) > 0.01:  # 0.01mm tolerance
                    disc = self._analyze_width_discontinuity(seg, next_seg, net_name)
                    if disc and disc.mismatch_percent >= self.MISMATCH_WARN:
                        discontinuities.append(disc)

            # Check for via discontinuities
            vias = list(board.vias_in_net(net_number))
            for via in vias:
                # Vias inherently cause impedance discontinuities
                disc = self._analyze_via_discontinuity(via, segments, net_name)
                if disc:
                    discontinuities.append(disc)

        # Sort by mismatch percentage (highest first)
        discontinuities.sort(key=lambda d: -d.mismatch_percent)

        return discontinuities

    def _identify_high_speed_nets(self, board: PCB) -> set[int]:
        """Identify high-speed nets by name pattern.

        Args:
            board: PCB object to analyze.

        Returns:
            Set of net numbers matching high-speed patterns.
        """
        high_speed: set[int] = set()

        for net in board.nets.values():
            if not net.name:
                continue

            for pattern in self._compiled_patterns:
                if pattern.search(net.name):
                    high_speed.add(net.number)
                    break

        return high_speed

    def _build_track_runs(
        self,
        board: PCB,
        net_number: int,
        net_name: str,
    ) -> dict[str, list[_TrackRun]]:
        """Build track runs grouped by layer.

        Args:
            board: PCB to analyze.
            net_number: Net number to build runs for.
            net_name: Net name for the run.

        Returns:
            Dict mapping layer name to list of track runs on that layer.
        """
        layer_runs: dict[str, list[_TrackRun]] = {}

        for segment in board.segments_in_net(net_number):
            layer = segment.layer

            if layer not in layer_runs:
                layer_runs[layer] = []

            # Calculate segment length
            dx = segment.end[0] - segment.start[0]
            dy = segment.end[1] - segment.start[1]
            length = math.sqrt(dx * dx + dy * dy)

            # Create a track run for this segment
            # (A more sophisticated implementation would merge connected segments)
            run = _TrackRun(
                segments=[segment],
                net_number=net_number,
                net_name=net_name,
                layer=layer,
                start=segment.start,
                end=segment.end,
                total_length=length,
            )
            layer_runs[layer].append(run)

        return layer_runs

    def _find_adjacent_tracks(
        self,
        board: PCB,
        run: _TrackRun,
        net_names: dict[int, str],
    ) -> list[tuple[_TrackRun, float]]:
        """Find tracks from other nets that run parallel and close.

        Args:
            board: PCB to search.
            run: Track run to find neighbors for.
            net_names: Mapping of net numbers to names.

        Returns:
            List of (adjacent_run, spacing_mm) tuples.
        """
        adjacent: list[tuple[_TrackRun, float]] = []

        for segment in board.segments_on_layer(run.layer):
            # Skip same net
            if segment.net_number == run.net_number:
                continue

            # Calculate parallel length and spacing
            parallel_length, spacing = self._calculate_coupling_geometry(run.segments[0], segment)

            if parallel_length < self.min_parallel_length:
                continue

            if spacing > self.max_coupling_distance:
                continue

            # Create a run for this adjacent segment
            adj_net_name = net_names.get(segment.net_number, f"Net{segment.net_number}")
            dx = segment.end[0] - segment.start[0]
            dy = segment.end[1] - segment.start[1]
            length = math.sqrt(dx * dx + dy * dy)

            adj_run = _TrackRun(
                segments=[segment],
                net_number=segment.net_number,
                net_name=adj_net_name,
                layer=segment.layer,
                start=segment.start,
                end=segment.end,
                total_length=length,
            )
            adjacent.append((adj_run, spacing))

        return adjacent

    def _calculate_coupling_geometry(
        self,
        seg1: Segment,
        seg2: Segment,
    ) -> tuple[float, float]:
        """Calculate parallel length and spacing between two segments.

        Args:
            seg1: First segment.
            seg2: Second segment.

        Returns:
            Tuple of (parallel_length_mm, edge_spacing_mm).
        """
        # Vector for seg1
        dx1 = seg1.end[0] - seg1.start[0]
        dy1 = seg1.end[1] - seg1.start[1]
        len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)

        # Vector for seg2
        dx2 = seg2.end[0] - seg2.start[0]
        dy2 = seg2.end[1] - seg2.start[1]
        len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

        if len1 < 0.01 or len2 < 0.01:
            return 0.0, float("inf")

        # Normalize vectors
        nx1, ny1 = dx1 / len1, dy1 / len1
        nx2, ny2 = dx2 / len2, dy2 / len2

        # Check if approximately parallel (dot product close to +/-1)
        dot = abs(nx1 * nx2 + ny1 * ny2)
        if dot < 0.9:  # Not parallel enough
            return 0.0, float("inf")

        # Calculate perpendicular distance between track centerlines
        # Project seg2.start onto seg1's line
        px = seg2.start[0] - seg1.start[0]
        py = seg2.start[1] - seg1.start[1]

        # Distance along seg1's direction
        proj_along = px * nx1 + py * ny1

        # Perpendicular distance (center-to-center)
        perp_dist = abs(px * (-ny1) + py * nx1)

        # Edge-to-edge spacing = center distance - half widths
        spacing = perp_dist - (seg1.width + seg2.width) / 2
        spacing = max(0.0, spacing)  # Can't be negative

        # Parallel overlap length (simplified - uses projection)
        overlap_start = max(0, proj_along)
        overlap_end = min(len1, proj_along + len2)
        parallel_length = max(0, overlap_end - overlap_start)

        return parallel_length, spacing

    def _calculate_crosstalk_risk(
        self,
        run1: _TrackRun,
        run2: _TrackRun,
        spacing: float,
        net1_name: str,
        net_names: dict[int, str],
    ) -> CrosstalkRisk:
        """Calculate crosstalk coupling between parallel tracks.

        Uses a simplified coupling model where coupling increases with
        parallel length and decreases with spacing.

        Args:
            run1: First track run (aggressor).
            run2: Second track run (victim).
            spacing: Edge-to-edge spacing in mm.
            net1_name: Name of first net.
            net_names: Mapping of net numbers to names.

        Returns:
            CrosstalkRisk assessment.
        """
        net2_name = net_names.get(run2.net_number, run2.net_name)
        parallel_length = min(run1.total_length, run2.total_length)

        # Simplified coupling model
        # Real model would consider dielectric, frequency, rise time, etc.
        # Coupling roughly proportional to length and inversely to spacing
        if spacing < 0.05:
            spacing = 0.05  # Minimum 50um for calculation

        # Empirical coupling coefficient formula
        # Based on coupled line theory (simplified)
        coupling = (parallel_length / 10.0) * (0.1 / spacing)
        coupling = min(coupling, 1.0)  # Cap at 1.0

        # Determine risk level
        if coupling >= self.COUPLING_HIGH:
            risk_level = RiskLevel.HIGH
        elif coupling >= self.COUPLING_MEDIUM:
            risk_level = RiskLevel.MEDIUM
        else:
            risk_level = RiskLevel.LOW

        # Generate suggestion
        suggestion = None
        if risk_level != RiskLevel.LOW:
            target_spacing = spacing * 2
            if target_spacing < 0.5:
                target_spacing = 0.5
            suggestion = f"Increase spacing to {target_spacing:.2f}mm or add ground guard trace"

        return CrosstalkRisk(
            aggressor_net=net1_name,
            victim_net=net2_name,
            parallel_length_mm=parallel_length,
            spacing_mm=spacing,
            layer=run1.layer,
            coupling_coefficient=coupling,
            risk_level=risk_level,
            suggestion=suggestion,
        )

    def _segments_connected(self, seg1: Segment, seg2: Segment) -> bool:
        """Check if two segments share an endpoint.

        Args:
            seg1: First segment.
            seg2: Second segment.

        Returns:
            True if segments share any endpoint within tolerance.
        """
        tolerance = 0.01  # 10 microns

        endpoints = [
            (seg1.start, seg2.start),
            (seg1.start, seg2.end),
            (seg1.end, seg2.start),
            (seg1.end, seg2.end),
        ]

        for p1, p2 in endpoints:
            dist = math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)
            if dist < tolerance:
                return True

        return False

    def _analyze_width_discontinuity(
        self,
        seg1: Segment,
        seg2: Segment,
        net_name: str,
    ) -> ImpedanceDiscontinuity | None:
        """Analyze impedance change from width difference.

        Uses a simplified impedance model where Z0 is inversely proportional
        to width for microstrip traces.

        Args:
            seg1: First segment (before).
            seg2: Second segment (after).
            net_name: Name of the net.

        Returns:
            ImpedanceDiscontinuity if significant, None otherwise.
        """
        # Find connection point
        position = self._find_connection_point(seg1, seg2)
        if position is None:
            return None

        # Simplified impedance model (proportional to 1/width)
        # Real model would use transmission line equations
        # Assuming 50 ohm nominal for 0.2mm width trace
        nominal_width = 0.2  # mm
        nominal_z0 = 50.0  # ohms

        z1 = nominal_z0 * (nominal_width / seg1.width) if seg1.width > 0 else 50.0
        z2 = nominal_z0 * (nominal_width / seg2.width) if seg2.width > 0 else 50.0

        mismatch = abs(z2 - z1) / z1 * 100 if z1 > 0 else 0

        if mismatch < self.MISMATCH_WARN:
            return None

        # Suggestion
        avg_width = (seg1.width + seg2.width) / 2
        suggestion = (
            f"Use consistent {avg_width:.3f}mm width to maintain {nominal_z0:.0f}Ohm impedance"
        )

        return ImpedanceDiscontinuity(
            net=net_name,
            position=position,
            impedance_before=z1,
            impedance_after=z2,
            mismatch_percent=mismatch,
            cause="width_change",
            suggestion=suggestion,
        )

    def _analyze_via_discontinuity(
        self,
        via,
        segments: list[Segment],
        net_name: str,
    ) -> ImpedanceDiscontinuity | None:
        """Analyze impedance discontinuity from a via.

        Vias introduce inherent impedance discontinuities due to their
        geometry differing from trace geometry.

        Args:
            via: Via to analyze.
            segments: Segments in the same net.
            net_name: Name of the net.

        Returns:
            ImpedanceDiscontinuity for the via.
        """
        # Vias typically have lower impedance than traces
        # Estimate ~30 ohm for a typical via vs ~50 ohm trace
        via_z0 = 30.0  # Approximate via impedance
        trace_z0 = 50.0  # Nominal trace impedance

        # Find average trace width near the via for better estimate
        nearby_widths = []
        for seg in segments:
            # Check if segment endpoint is near the via
            for point in [seg.start, seg.end]:
                dist = math.sqrt(
                    (point[0] - via.position[0]) ** 2 + (point[1] - via.position[1]) ** 2
                )
                if dist < 1.0:  # Within 1mm
                    nearby_widths.append(seg.width)

        if nearby_widths:
            avg_width = sum(nearby_widths) / len(nearby_widths)
            # Adjust trace Z0 estimate based on width
            trace_z0 = 50.0 * (0.2 / avg_width) if avg_width > 0 else 50.0

        mismatch = abs(via_z0 - trace_z0) / trace_z0 * 100

        # Only report significant via discontinuities
        if mismatch < 20:  # Vias are expected to have some mismatch
            return None

        suggestion = "Consider via-in-pad or back-drill for high-speed signals"

        return ImpedanceDiscontinuity(
            net=net_name,
            position=via.position,
            impedance_before=trace_z0,
            impedance_after=via_z0,
            mismatch_percent=mismatch,
            cause="via",
            suggestion=suggestion,
        )

    def _find_connection_point(
        self,
        seg1: Segment,
        seg2: Segment,
    ) -> tuple[float, float] | None:
        """Find the shared connection point between two segments.

        Args:
            seg1: First segment.
            seg2: Second segment.

        Returns:
            (x, y) position of connection, or None if not connected.
        """
        tolerance = 0.01

        checks = [
            (seg1.start, seg2.start, seg1.start),
            (seg1.start, seg2.end, seg1.start),
            (seg1.end, seg2.start, seg1.end),
            (seg1.end, seg2.end, seg1.end),
        ]

        for p1, p2, result in checks:
            dist = math.sqrt((p1[0] - p2[0]) ** 2 + (p1[1] - p2[1]) ** 2)
            if dist < tolerance:
                return result

        return None
