"""Routing congestion analysis for PCB designs.

Analyzes routing congestion to identify problem areas and suggest solutions.
Uses grid-based density analysis to find hotspots where tracks, vias,
and components are concentrated.

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import CongestionAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = CongestionAnalyzer()
    >>> reports = analyzer.analyze(pcb)
    >>> for report in reports:
    ...     print(f"{report.severity}: Area around {report.center}")
    ...     for suggestion in report.suggestions:
    ...         print(f"  - {suggestion}")
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class Severity(Enum):
    """Congestion severity level."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class CongestionReport:
    """Report on routing congestion in an area.

    Attributes:
        center: Center point (x, y) of the congested area in mm.
        radius: Radius of the analyzed area in mm.
        track_density: Track length per unit area (mm/mm²).
        via_count: Number of vias in the area.
        unrouted_connections: Number of pads without connections.
        components: Reference designators of components in the area.
        nets: Net names involved in the congestion.
        severity: Severity level of the congestion.
        suggestions: List of actionable suggestions to relieve congestion.
    """

    center: tuple[float, float]
    radius: float

    # Metrics
    track_density: float  # mm of track per mm²
    via_count: int
    unrouted_connections: int

    # Elements contributing to congestion
    components: list[str] = field(default_factory=list)
    nets: list[str] = field(default_factory=list)

    # Severity
    severity: Severity = Severity.LOW

    # Suggestions
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "center": {"x": self.center[0], "y": self.center[1]},
            "radius": self.radius,
            "track_density": round(self.track_density, 3),
            "via_count": self.via_count,
            "unrouted_connections": self.unrouted_connections,
            "components": self.components,
            "nets": self.nets,
            "severity": self.severity.value,
            "suggestions": self.suggestions,
        }


@dataclass
class _GridCell:
    """Internal grid cell for density analysis."""

    x: int  # Grid cell x index
    y: int  # Grid cell y index
    center_x: float  # Center x in mm
    center_y: float  # Center y in mm
    track_length: float = 0.0  # Total track length in mm
    via_count: int = 0
    pad_count: int = 0
    connected_pads: int = 0
    components: set[str] = field(default_factory=set)
    nets: set[int] = field(default_factory=set)


class CongestionAnalyzer:
    """Analyze routing congestion on a PCB.

    Uses a grid-based approach to identify areas of high track density,
    excessive vias, and unrouted connections. Generates actionable
    suggestions for relieving congestion.

    Args:
        grid_size: Size of each grid cell in mm. Default 2.0mm.
        merge_radius: Radius for merging adjacent hotspots in mm. Default 5.0mm.
        max_workers: Maximum number of worker threads for parallel grid
            processing. Defaults to CPU count. Set to 1 to disable parallelism.
    """

    # Density thresholds for severity classification (mm track per mm² area)
    DENSITY_LOW = 0.5
    DENSITY_MEDIUM = 1.0
    DENSITY_HIGH = 1.5
    DENSITY_CRITICAL = 2.0

    # Via count thresholds per grid cell
    VIA_LOW = 2
    VIA_MEDIUM = 5
    VIA_HIGH = 8
    VIA_CRITICAL = 12

    # Minimum number of cells to benefit from parallelism
    _PARALLEL_THRESHOLD = 100

    def __init__(
        self,
        grid_size: float = 2.0,
        merge_radius: float = 5.0,
        max_workers: int | None = None,
    ):
        """Initialize the analyzer.

        Args:
            grid_size: Size of each grid cell in mm.
            merge_radius: Radius for merging adjacent hotspots in mm.
            max_workers: Maximum number of worker threads for parallel grid
                processing. Defaults to CPU count. Set to 1 to disable parallelism.
        """
        self.grid_size = grid_size
        self.merge_radius = merge_radius
        self.max_workers = max_workers if max_workers is not None else (os.cpu_count() or 1)

    def analyze(self, board: PCB) -> list[CongestionReport]:
        """Find congested areas on the board.

        Args:
            board: PCB object to analyze.

        Returns:
            List of CongestionReport objects for each congested area,
            sorted by severity (most severe first).
        """
        # Build the net name lookup
        net_names = {net.number: net.name for net in board.nets.values()}

        # Create density grid
        grid = self._create_density_grid(board)

        # Find hotspot cells
        hotspots = self._find_hotspots(grid)

        # Merge adjacent hotspots and create reports
        reports = []
        for hotspot in self._merge_hotspots(hotspots):
            report = self._create_report(hotspot, net_names)
            report.suggestions = self._suggest_fixes(report, board)
            reports.append(report)

        # Sort by severity (critical first)
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        reports.sort(key=lambda r: severity_order[r.severity])

        return reports

    def _create_density_grid(self, board: PCB) -> dict[tuple[int, int], _GridCell]:
        """Create a grid of cells with density metrics.

        Args:
            board: PCB to analyze.

        Returns:
            Dictionary mapping (x, y) grid indices to GridCell objects.
        """
        grid: dict[tuple[int, int], _GridCell] = {}

        def get_cell(x: float, y: float) -> _GridCell:
            """Get or create the grid cell for a point."""
            gx = int(x // self.grid_size)
            gy = int(y // self.grid_size)
            key = (gx, gy)
            if key not in grid:
                grid[key] = _GridCell(
                    x=gx,
                    y=gy,
                    center_x=(gx + 0.5) * self.grid_size,
                    center_y=(gy + 0.5) * self.grid_size,
                )
            return grid[key]

        # Process segments (tracks)
        for segment in board.segments:
            # Calculate segment length
            dx = segment.end[0] - segment.start[0]
            dy = segment.end[1] - segment.start[1]
            length = math.sqrt(dx * dx + dy * dy)

            if length < 0.01:
                continue  # Skip zero-length segments

            # Distribute track length across all cells the segment passes through
            # Sample at regular intervals (every grid_size/2 for good coverage)
            num_samples = max(2, int(length / (self.grid_size / 2)) + 1)
            length_per_sample = length / num_samples

            for i in range(num_samples):
                t = i / (num_samples - 1) if num_samples > 1 else 0.5
                px = segment.start[0] + t * dx
                py = segment.start[1] + t * dy
                cell = get_cell(px, py)
                cell.track_length += length_per_sample
                cell.nets.add(segment.net_number)

        # Process vias
        for via in board.vias:
            cell = get_cell(via.position[0], via.position[1])
            cell.via_count += 1
            cell.nets.add(via.net_number)

        # Process footprints and pads
        for footprint in board.footprints:
            fx, fy = footprint.position
            cell = get_cell(fx, fy)
            cell.components.add(footprint.reference)

            for pad in footprint.pads:
                # Transform pad position to board coordinates
                # (simplified - assumes no rotation for now)
                pad_x = fx + pad.position[0]
                pad_y = fy + pad.position[1]
                pad_cell = get_cell(pad_x, pad_y)
                pad_cell.pad_count += 1
                if pad.net_number != 0:
                    pad_cell.connected_pads += 1
                    pad_cell.nets.add(pad.net_number)

        return grid

    def _find_hotspots(self, grid: dict[tuple[int, int], _GridCell]) -> list[_GridCell]:
        """Find cells with significant congestion.

        Uses parallel processing for large grids to improve performance.

        Args:
            grid: Grid of density cells.

        Returns:
            List of cells that exceed congestion thresholds.
        """
        cells = list(grid.values())
        num_cells = len(cells)

        # Use sequential processing for small grids or when parallelism disabled
        if self.max_workers <= 1 or num_cells < self._PARALLEL_THRESHOLD:
            return self._find_hotspots_sequential(cells)

        return self._find_hotspots_parallel(cells)

    def _is_hotspot(self, cell: _GridCell) -> bool:
        """Check if a single cell qualifies as a hotspot.

        This method is designed to be called in parallel - it only reads
        cell data and class constants, doesn't modify any state.

        Args:
            cell: Grid cell to evaluate.

        Returns:
            True if the cell exceeds congestion thresholds.
        """
        cell_area = self.grid_size * self.grid_size
        density = cell.track_length / cell_area

        return (
            density >= self.DENSITY_LOW
            or cell.via_count >= self.VIA_LOW
            or (cell.pad_count > 0 and cell.connected_pads < cell.pad_count)
        )

    def _find_hotspots_sequential(self, cells: list[_GridCell]) -> list[_GridCell]:
        """Find hotspots using sequential processing.

        Args:
            cells: List of grid cells to check.

        Returns:
            List of cells that exceed congestion thresholds.
        """
        return [cell for cell in cells if self._is_hotspot(cell)]

    def _check_cell_chunk(self, chunk: list[_GridCell]) -> list[_GridCell]:
        """Check a chunk of cells for hotspots.

        Args:
            chunk: List of cells to check.

        Returns:
            List of cells from the chunk that are hotspots.
        """
        return [cell for cell in chunk if self._is_hotspot(cell)]

    def _find_hotspots_parallel(self, cells: list[_GridCell]) -> list[_GridCell]:
        """Find hotspots using parallel processing.

        Divides cells into chunks and processes them in parallel using
        ThreadPoolExecutor.

        Args:
            cells: List of grid cells to check.

        Returns:
            List of cells that exceed congestion thresholds.
        """
        # Calculate optimal chunk size: balance between parallelism and overhead
        # Use approximately 4 chunks per worker for good load balancing
        num_chunks = self.max_workers * 4
        chunk_size = max(1, len(cells) // num_chunks)

        # Create chunks
        chunks = [cells[i : i + chunk_size] for i in range(0, len(cells), chunk_size)]

        # Process chunks in parallel
        hotspots: list[_GridCell] = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            chunk_results = executor.map(self._check_cell_chunk, chunks)

            # Flatten results
            for chunk_hotspots in chunk_results:
                hotspots.extend(chunk_hotspots)

        return hotspots

    def _merge_hotspots(self, cells: list[_GridCell]) -> list[_GridCell]:
        """Merge adjacent hotspot cells into larger regions.

        For now, returns cells as-is. Future enhancement could
        cluster adjacent cells into unified regions.

        Args:
            cells: List of hotspot cells.

        Returns:
            List of representative cells (potentially merged).
        """
        # Simple implementation: return top N hotspots by density
        # Sort by combined score
        cell_area = self.grid_size * self.grid_size

        def score(cell: _GridCell) -> float:
            density = cell.track_length / cell_area
            return density + (cell.via_count * 0.1)

        cells_sorted = sorted(cells, key=score, reverse=True)

        # Return top 10 hotspots, filtering out nearby duplicates
        result = []
        for cell in cells_sorted:
            # Check if too close to an existing result
            too_close = False
            for existing in result:
                dist = math.sqrt(
                    (cell.center_x - existing.center_x) ** 2
                    + (cell.center_y - existing.center_y) ** 2
                )
                if dist < self.merge_radius:
                    # Merge into existing
                    existing.track_length += cell.track_length
                    existing.via_count += cell.via_count
                    existing.components.update(cell.components)
                    existing.nets.update(cell.nets)
                    too_close = True
                    break

            if not too_close:
                result.append(cell)

            if len(result) >= 10:
                break

        return result

    def _create_report(self, cell: _GridCell, net_names: dict[int, str]) -> CongestionReport:
        """Create a CongestionReport from a grid cell.

        Args:
            cell: Grid cell with congestion metrics.
            net_names: Mapping of net numbers to names.

        Returns:
            CongestionReport for the cell.
        """
        cell_area = self.grid_size * self.grid_size
        density = cell.track_length / cell_area

        # Determine severity
        if density >= self.DENSITY_CRITICAL or cell.via_count >= self.VIA_CRITICAL:
            severity = Severity.CRITICAL
        elif density >= self.DENSITY_HIGH or cell.via_count >= self.VIA_HIGH:
            severity = Severity.HIGH
        elif density >= self.DENSITY_MEDIUM or cell.via_count >= self.VIA_MEDIUM:
            severity = Severity.MEDIUM
        else:
            severity = Severity.LOW

        # Get net names
        nets = [
            net_names.get(n, f"net_{n}")
            for n in sorted(cell.nets)
            if n != 0  # Skip unconnected net
        ]

        # Calculate unrouted connections
        unrouted = cell.pad_count - cell.connected_pads

        return CongestionReport(
            center=(round(cell.center_x, 2), round(cell.center_y, 2)),
            radius=self.grid_size,
            track_density=round(density, 3),
            via_count=cell.via_count,
            unrouted_connections=max(0, unrouted),
            components=sorted(cell.components),
            nets=nets[:10],  # Limit to 10 nets
            severity=severity,
        )

    def _suggest_fixes(self, report: CongestionReport, board: PCB) -> list[str]:
        """Generate suggestions to relieve congestion.

        Args:
            report: Congestion report to generate suggestions for.
            board: PCB for context.

        Returns:
            List of actionable suggestion strings.
        """
        suggestions = []

        # Suggest moving components if there are many in the area
        if len(report.components) >= 2:
            comp_list = ", ".join(report.components[:3])
            if len(report.components) > 3:
                comp_list += f" (and {len(report.components) - 3} more)"
            suggestions.append(f"Consider moving {comp_list} to reduce component density")

        # Suggest layer changes for high density
        if report.severity in (Severity.HIGH, Severity.CRITICAL):
            suggestions.append("Route some nets on inner layers to reduce top/bottom congestion")

        # Suggest via reduction
        if report.via_count >= 10:
            suggestions.append(
                f"Area has {report.via_count} vias; consider optimizing routing "
                "to reduce layer changes"
            )
        elif report.via_count >= 5:
            suggestions.append(
                f"Consider reducing vias ({report.via_count}) by routing on fewer layers"
            )

        # Suggest addressing unrouted connections
        if report.unrouted_connections > 0:
            suggestions.append(
                f"{report.unrouted_connections} unrouted connection(s) in this area; "
                "may need manual routing or component repositioning"
            )

        # Suggest specific net routing if there are many nets
        if len(report.nets) >= 5:
            # Find power/ground nets
            power_nets = [
                n
                for n in report.nets
                if any(p in n.upper() for p in ["VCC", "VDD", "GND", "VSS", "PWR"])
            ]
            if power_nets:
                suggestions.append(
                    f"Power nets ({', '.join(power_nets[:3])}) could use wider "
                    "traces or dedicated planes"
                )

        # Suggest via-in-pad for bypass caps
        bypass_refs = [r for r in report.components if r.startswith("C")]
        if bypass_refs and report.severity in (Severity.HIGH, Severity.CRITICAL):
            suggestions.append(
                f"Consider via-in-pad for bypass capacitors ({', '.join(bypass_refs[:3])})"
            )

        # Generic suggestion for critical areas
        if report.severity == Severity.CRITICAL and not suggestions:
            suggestions.append(
                "Critical congestion: consider redesigning component placement "
                "or adding board layers"
            )

        return suggestions
