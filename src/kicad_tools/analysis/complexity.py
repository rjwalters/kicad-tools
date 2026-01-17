"""Pre-routing complexity estimation and layer prediction.

Analyzes PCB design before routing to estimate:
- Routing complexity score
- Minimum layer count needed
- Success probability for different layer configurations
- Bottleneck identification

Example:
    >>> from kicad_tools.schema.pcb import PCB
    >>> from kicad_tools.analysis import ComplexityAnalyzer
    >>> pcb = PCB.load("board.kicad_pcb")
    >>> analyzer = ComplexityAnalyzer()
    >>> report = analyzer.analyze(pcb)
    >>> print(f"Complexity: {report.complexity_rating}")
    >>> print(f"Recommended layers: {report.min_layers_predicted}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


class ComplexityRating(Enum):
    """Overall routing complexity rating."""

    TRIVIAL = "trivial"  # Simple board, 2 layers easy
    SIMPLE = "simple"  # Straightforward, 2 layers likely
    MODERATE = "moderate"  # Some challenges, 2 layers possible
    COMPLEX = "complex"  # Significant challenges, 4 layers likely needed
    EXTREME = "extreme"  # Very challenging, 6+ layers recommended


@dataclass
class Bottleneck:
    """Identified routing bottleneck.

    Represents a congested area or challenging component that may
    block successful routing.
    """

    component_ref: str
    position: tuple[float, float]
    description: str
    pin_count: int = 0
    pin_density: float = 0.0  # pins per mm²
    available_channels: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "component": self.component_ref,
            "position": {"x": self.position[0], "y": self.position[1]},
            "description": self.description,
            "pin_count": self.pin_count,
            "pin_density": round(self.pin_density, 3),
            "available_channels": self.available_channels,
        }


@dataclass
class LayerPrediction:
    """Success prediction for a specific layer count."""

    layer_count: int
    success_probability: float  # 0.0 to 1.0
    recommended: bool
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "layers": self.layer_count,
            "probability": round(self.success_probability, 2),
            "recommended": self.recommended,
            "notes": self.notes,
        }


@dataclass
class RoutingComplexity:
    """Complete routing complexity analysis report.

    Attributes:
        total_pads: Total number of pads on the board
        total_nets: Total number of nets requiring routing
        board_area_mm2: Board area in square millimeters
        avg_net_length_mm: Average estimated net length
        max_pin_density: Maximum pin density in any region
        crossing_number: Estimated net crossing count

        density_score: Pad density score (0-100)
        crossing_score: Crossing complexity score (0-100)
        channel_score: Available routing channels score (0-100)
        overall_score: Combined complexity score (0-100)

        complexity_rating: Overall rating (trivial to extreme)
        min_layers_predicted: Minimum recommended layer count
        layer_predictions: Success predictions per layer count
        bottlenecks: Identified routing bottlenecks
    """

    # Raw metrics
    total_pads: int = 0
    total_nets: int = 0
    board_area_mm2: float = 0.0
    board_width_mm: float = 0.0
    board_height_mm: float = 0.0
    avg_net_length_mm: float = 0.0
    max_pin_density: float = 0.0
    crossing_number: int = 0
    differential_pair_count: int = 0
    high_speed_net_count: int = 0

    # Derived scores (0-100)
    density_score: float = 0.0
    crossing_score: float = 0.0
    channel_score: float = 0.0
    overall_score: float = 0.0

    # Predictions
    complexity_rating: ComplexityRating = ComplexityRating.TRIVIAL
    min_layers_predicted: int = 2
    layer_predictions: list[LayerPrediction] = field(default_factory=list)
    bottlenecks: list[Bottleneck] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "metrics": {
                "total_pads": self.total_pads,
                "total_nets": self.total_nets,
                "board_area_mm2": round(self.board_area_mm2, 1),
                "board_width_mm": round(self.board_width_mm, 1),
                "board_height_mm": round(self.board_height_mm, 1),
                "avg_net_length_mm": round(self.avg_net_length_mm, 2),
                "max_pin_density": round(self.max_pin_density, 3),
                "crossing_number": self.crossing_number,
                "differential_pairs": self.differential_pair_count,
                "high_speed_nets": self.high_speed_net_count,
            },
            "scores": {
                "density": round(self.density_score, 1),
                "crossings": round(self.crossing_score, 1),
                "channels": round(self.channel_score, 1),
                "overall": round(self.overall_score, 1),
            },
            "predictions": {
                "complexity_rating": self.complexity_rating.value,
                "min_layers_predicted": self.min_layers_predicted,
                "layer_predictions": [p.to_dict() for p in self.layer_predictions],
            },
            "bottlenecks": [b.to_dict() for b in self.bottlenecks],
            "recommendations": self.recommendations,
        }


@dataclass
class _GridCell:
    """Internal grid cell for density analysis."""

    x: int
    y: int
    center_x: float
    center_y: float
    pad_count: int = 0
    net_count: int = 0
    nets: set[int] = field(default_factory=set)
    components: set[str] = field(default_factory=set)


class ComplexityAnalyzer:
    """Analyze routing complexity before attempting to route.

    Uses heuristics based on:
    - Pad density and distribution
    - Net crossing estimation
    - Available routing channels
    - Component pin density

    Args:
        grid_size: Size of analysis grid cells in mm. Default 5.0mm.
    """

    # Thresholds for complexity scoring
    DENSITY_THRESHOLDS = {
        "trivial": 0.01,  # pads/mm²
        "simple": 0.02,
        "moderate": 0.035,
        "complex": 0.05,
        "extreme": 0.07,
    }

    # Average crossing thresholds (crossings per net)
    CROSSING_THRESHOLDS = {
        "trivial": 0.5,
        "simple": 1.5,
        "moderate": 3.0,
        "complex": 5.0,
        "extreme": 8.0,
    }

    # High-density component thresholds (pins/mm² for QFP-like packages)
    HIGH_DENSITY_THRESHOLD = 0.5  # pins per mm²
    VERY_HIGH_DENSITY_THRESHOLD = 1.0

    def __init__(self, grid_size: float = 5.0):
        """Initialize the analyzer.

        Args:
            grid_size: Size of analysis grid cells in mm.
        """
        self.grid_size = grid_size

    def analyze(self, board: PCB) -> RoutingComplexity:
        """Analyze board routing complexity.

        Args:
            board: PCB object to analyze.

        Returns:
            RoutingComplexity report with metrics, scores, and predictions.
        """
        report = RoutingComplexity()

        # Calculate basic metrics
        self._calculate_metrics(board, report)

        # Calculate complexity scores
        self._calculate_scores(report)

        # Predict layer requirements
        self._predict_layers(report)

        # Identify bottlenecks
        self._identify_bottlenecks(board, report)

        # Generate recommendations
        self._generate_recommendations(report)

        return report

    def _calculate_metrics(self, board: PCB, report: RoutingComplexity) -> None:
        """Calculate basic metrics from the board."""
        # Board dimensions
        width, height = self._get_board_size(board)
        report.board_width_mm = width
        report.board_height_mm = height
        report.board_area_mm2 = width * height

        # Pad and net counts
        total_pads = 0
        pad_positions: list[tuple[float, float, int]] = []  # (x, y, net)

        for footprint in board.footprints:
            fx, fy = footprint.position
            rotation = math.radians(footprint.rotation or 0)

            for pad in footprint.pads:
                # Transform pad position to board coordinates
                px, py = pad.position
                # Apply rotation
                rx = px * math.cos(rotation) - py * math.sin(rotation)
                ry = px * math.sin(rotation) + py * math.cos(rotation)
                # Apply translation
                board_x = fx + rx
                board_y = fy + ry

                total_pads += 1
                if pad.net_number > 0:
                    pad_positions.append((board_x, board_y, pad.net_number))

        report.total_pads = total_pads

        # Count multi-pad nets (nets that need routing)
        net_pad_counts: dict[int, int] = {}
        for _, _, net in pad_positions:
            net_pad_counts[net] = net_pad_counts.get(net, 0) + 1

        multi_pad_nets = [n for n, count in net_pad_counts.items() if count >= 2]
        report.total_nets = len(multi_pad_nets)

        # Estimate average net length using centroid distance
        net_centroids: dict[int, list[tuple[float, float]]] = {}
        for x, y, net in pad_positions:
            if net in multi_pad_nets:
                if net not in net_centroids:
                    net_centroids[net] = []
                net_centroids[net].append((x, y))

        total_length = 0.0
        for net_id, positions in net_centroids.items():
            if len(positions) >= 2:
                # Minimum spanning tree length estimate using sorted distances
                length = self._estimate_mst_length(positions)
                total_length += length

        if report.total_nets > 0:
            report.avg_net_length_mm = total_length / report.total_nets

        # Estimate crossing number using grid-based analysis
        grid = self._build_density_grid(board, pad_positions)
        report.crossing_number = self._estimate_crossings(grid, net_centroids)
        report.max_pin_density = self._get_max_pin_density(grid)

        # Count differential pairs and high-speed nets
        report.differential_pair_count = self._count_differential_pairs(board)
        report.high_speed_net_count = self._count_high_speed_nets(board)

    def _calculate_scores(self, report: RoutingComplexity) -> None:
        """Calculate complexity scores from metrics."""
        # Density score: based on pads per unit area
        if report.board_area_mm2 > 0:
            pad_density = report.total_pads / report.board_area_mm2
            report.density_score = self._normalize_score(
                pad_density,
                self.DENSITY_THRESHOLDS["trivial"],
                self.DENSITY_THRESHOLDS["extreme"],
            )
        else:
            report.density_score = 100.0

        # Crossing score: based on crossings per net
        if report.total_nets > 0:
            crossings_per_net = report.crossing_number / report.total_nets
            report.crossing_score = self._normalize_score(
                crossings_per_net,
                self.CROSSING_THRESHOLDS["trivial"],
                self.CROSSING_THRESHOLDS["extreme"],
            )
        else:
            report.crossing_score = 0.0

        # Channel score: inverse of max pin density (more density = fewer channels)
        # Scale: 0 density = 100 score, HIGH_DENSITY = 50, VERY_HIGH = 0
        report.channel_score = max(
            0.0,
            100.0 - (report.max_pin_density / self.VERY_HIGH_DENSITY_THRESHOLD) * 100.0,
        )

        # Overall score: weighted combination
        # Higher weight on density and crossings as primary indicators
        report.overall_score = (
            report.density_score * 0.4
            + report.crossing_score * 0.35
            + (100 - report.channel_score) * 0.25
        )

        # Determine complexity rating
        if report.overall_score < 20:
            report.complexity_rating = ComplexityRating.TRIVIAL
        elif report.overall_score < 40:
            report.complexity_rating = ComplexityRating.SIMPLE
        elif report.overall_score < 60:
            report.complexity_rating = ComplexityRating.MODERATE
        elif report.overall_score < 80:
            report.complexity_rating = ComplexityRating.COMPLEX
        else:
            report.complexity_rating = ComplexityRating.EXTREME

    def _predict_layers(self, report: RoutingComplexity) -> None:
        """Predict success probability for different layer counts."""
        score = report.overall_score
        has_diff_pairs = report.differential_pair_count > 0
        has_high_speed = report.high_speed_net_count > 0

        # 2-layer prediction
        prob_2 = self._layer_probability(score, base_layers=2)
        if has_diff_pairs or has_high_speed:
            prob_2 *= 0.8  # Reduce confidence for special routing needs

        # 4-layer prediction
        prob_4 = self._layer_probability(score, base_layers=4)

        # 6-layer prediction
        prob_6 = self._layer_probability(score, base_layers=6)

        # Build predictions
        predictions = []

        # 2-layer
        rec_2 = prob_2 >= 0.7
        notes_2 = ""
        if prob_2 < 0.3:
            notes_2 = "Not recommended"
        elif prob_2 < 0.7:
            notes_2 = "May require optimization"
        predictions.append(
            LayerPrediction(
                layer_count=2,
                success_probability=prob_2,
                recommended=rec_2,
                notes=notes_2,
            )
        )

        # 4-layer
        rec_4 = 0.3 <= prob_2 < 0.7 or (has_diff_pairs and prob_2 < 0.9)
        notes_4 = ""
        if has_diff_pairs:
            notes_4 = "Good for differential pairs"
        predictions.append(
            LayerPrediction(
                layer_count=4,
                success_probability=prob_4,
                recommended=rec_4,
                notes=notes_4,
            )
        )

        # 6-layer
        rec_6 = prob_4 < 0.7 or report.complexity_rating == ComplexityRating.EXTREME
        notes_6 = ""
        if rec_6:
            notes_6 = "Recommended for this complexity"
        predictions.append(
            LayerPrediction(
                layer_count=6,
                success_probability=prob_6,
                recommended=rec_6,
                notes=notes_6,
            )
        )

        report.layer_predictions = predictions

        # Determine minimum predicted layers
        if prob_2 >= 0.7:
            report.min_layers_predicted = 2
        elif prob_4 >= 0.7:
            report.min_layers_predicted = 4
        else:
            report.min_layers_predicted = 6

    def _identify_bottlenecks(self, board: PCB, report: RoutingComplexity) -> None:
        """Identify potential routing bottlenecks."""
        bottlenecks = []

        for footprint in board.footprints:
            pad_count = len(footprint.pads)
            if pad_count < 8:
                continue  # Skip small components

            # Calculate component bounding box
            min_x = min_y = float("inf")
            max_x = max_y = float("-inf")

            for pad in footprint.pads:
                px, py = pad.position
                min_x = min(min_x, px)
                min_y = min(min_y, py)
                max_x = max(max_x, px)
                max_y = max(max_y, py)

            width = max_x - min_x
            height = max_y - min_y
            area = width * height if width > 0 and height > 0 else 1.0

            pin_density = pad_count / area

            # Identify high-density packages
            if pin_density >= self.HIGH_DENSITY_THRESHOLD:
                # Estimate available routing channels
                # Assume 0.3mm minimum trace + clearance, calculate channels around perimeter
                perimeter = 2 * (width + height)
                channel_spacing = 0.5  # mm per channel
                available_channels = int(perimeter / channel_spacing)

                description = self._describe_bottleneck(
                    footprint.reference,
                    pad_count,
                    pin_density,
                )

                bottlenecks.append(
                    Bottleneck(
                        component_ref=footprint.reference,
                        position=footprint.position,
                        description=description,
                        pin_count=pad_count,
                        pin_density=pin_density,
                        available_channels=available_channels,
                    )
                )

        # Sort by pin density (worst first)
        bottlenecks.sort(key=lambda b: b.pin_density, reverse=True)
        report.bottlenecks = bottlenecks[:10]  # Top 10 bottlenecks

    def _generate_recommendations(self, report: RoutingComplexity) -> None:
        """Generate actionable recommendations."""
        recommendations = []

        # Layer recommendation
        if report.min_layers_predicted > 2:
            recommendations.append(
                f"Consider {report.min_layers_predicted}-layer board for reliable routing"
            )

        # High-density component advice
        if report.bottlenecks:
            worst = report.bottlenecks[0]
            recommendations.append(
                f"High pin density around {worst.component_ref} "
                f"({worst.pin_count} pins) - may need escape routing"
            )

        # Differential pair advice
        if report.differential_pair_count > 0:
            recommendations.append(
                f"Board has {report.differential_pair_count} differential pair(s) - "
                "ensure length matching and controlled impedance"
            )

        # Density-based advice
        if report.density_score > 70:
            recommendations.append(
                "High pad density - consider finer trace/clearance rules or larger board"
            )

        # Crossing-based advice
        if report.crossing_score > 70:
            recommendations.append(
                "High net crossing complexity - additional layers or component "
                "repositioning may help"
            )

        # Auto-layers suggestion for complex boards
        if report.overall_score > 50:
            recommendations.append(
                "Use --auto-layers flag to automatically find minimum viable layer count"
            )

        report.recommendations = recommendations

    # ---- Helper Methods ----

    def _get_board_size(self, board: PCB) -> tuple[float, float]:
        """Get board dimensions from edge cuts."""
        # Try to find edge cuts using the PCB's public API
        edge_coords: list[tuple[float, float]] = []

        for line in board.graphic_lines:
            if line.layer == "Edge.Cuts":
                edge_coords.append(line.start)
                edge_coords.append(line.end)

        for arc in board.graphic_arcs:
            if arc.layer == "Edge.Cuts":
                edge_coords.append(arc.start)
                edge_coords.append(arc.mid)
                edge_coords.append(arc.end)

        if edge_coords:
            xs = [c[0] for c in edge_coords]
            ys = [c[1] for c in edge_coords]
            return (max(xs) - min(xs), max(ys) - min(ys))

        return (100.0, 100.0)  # Default

    def _build_density_grid(
        self,
        board: PCB,
        pad_positions: list[tuple[float, float, int]],
    ) -> dict[tuple[int, int], _GridCell]:
        """Build a grid for density analysis."""
        grid: dict[tuple[int, int], _GridCell] = {}

        def get_cell(x: float, y: float) -> _GridCell:
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

        for x, y, net in pad_positions:
            cell = get_cell(x, y)
            cell.pad_count += 1
            if net > 0:
                cell.nets.add(net)
                cell.net_count = len(cell.nets)

        return grid

    def _estimate_crossings(
        self,
        grid: dict[tuple[int, int], _GridCell],
        net_centroids: dict[int, list[tuple[float, float]]],
    ) -> int:
        """Estimate net crossing count using grid analysis.

        Uses a heuristic based on:
        - Number of cells with multiple nets
        - Manhattan distance between net endpoints
        """
        crossings = 0

        for cell in grid.values():
            if cell.net_count >= 2:
                # Each pair of nets in a cell could cross
                crossings += (cell.net_count * (cell.net_count - 1)) // 2

        return crossings

    def _get_max_pin_density(self, grid: dict[tuple[int, int], _GridCell]) -> float:
        """Get maximum pin density from any grid cell."""
        if not grid:
            return 0.0

        cell_area = self.grid_size * self.grid_size
        max_density = 0.0

        for cell in grid.values():
            density = cell.pad_count / cell_area
            max_density = max(max_density, density)

        return max_density

    def _estimate_mst_length(self, positions: list[tuple[float, float]]) -> float:
        """Estimate minimum spanning tree length for a set of positions.

        Uses a simple heuristic: sum of nearest-neighbor distances.
        """
        if len(positions) < 2:
            return 0.0

        # Simple nearest-neighbor approximation
        remaining = list(positions)
        current = remaining.pop(0)
        total = 0.0

        while remaining:
            # Find nearest point
            min_dist = float("inf")
            nearest_idx = 0

            for i, pos in enumerate(remaining):
                dx = pos[0] - current[0]
                dy = pos[1] - current[1]
                dist = math.sqrt(dx * dx + dy * dy)
                if dist < min_dist:
                    min_dist = dist
                    nearest_idx = i

            total += min_dist
            current = remaining.pop(nearest_idx)

        return total

    def _count_differential_pairs(self, board: PCB) -> int:
        """Count differential pair nets."""
        count = 0
        seen_bases: set[str] = set()

        for net in board.nets.values():
            name = net.name.upper()
            # Common differential pair patterns
            for pattern in ["_P", "_N", "+", "-", "_DP", "_DN", "_POS", "_NEG"]:
                if name.endswith(pattern):
                    base = name[: -len(pattern)]
                    if base not in seen_bases:
                        seen_bases.add(base)
                        count += 1
                    break

        return count

    def _count_high_speed_nets(self, board: PCB) -> int:
        """Count high-speed signal nets."""
        count = 0
        high_speed_patterns = [
            "CLK",
            "CLOCK",
            "USB",
            "HDMI",
            "LVDS",
            "DDR",
            "PCIE",
            "SATA",
            "ETH",
            "SGMII",
        ]

        for net in board.nets.values():
            name = net.name.upper()
            if any(pattern in name for pattern in high_speed_patterns):
                count += 1

        return count

    def _normalize_score(self, value: float, low: float, high: float) -> float:
        """Normalize a value to 0-100 scale."""
        if high <= low:
            return 0.0
        normalized = (value - low) / (high - low)
        return min(100.0, max(0.0, normalized * 100))

    def _layer_probability(self, score: float, base_layers: int) -> float:
        """Calculate success probability for a layer count.

        Uses a sigmoid-like function adjusted for each layer count.
        """
        # Adjust score based on layer count
        # More layers = higher probability for same complexity
        adjustment = {
            2: 0,
            4: -30,  # 4 layers "feels like" 30 points lower complexity
            6: -55,  # 6 layers "feels like" 55 points lower complexity
        }

        adjusted_score = score + adjustment.get(base_layers, 0)

        # Convert to probability using sigmoid
        # Score 0 -> ~99%, Score 50 -> ~50%, Score 100 -> ~1%
        if adjusted_score <= 0:
            return 0.99
        elif adjusted_score >= 100:
            return 0.01

        # Sigmoid transformation
        probability = 1.0 / (1.0 + math.exp((adjusted_score - 50) / 15))
        return round(probability, 2)

    def _describe_bottleneck(
        self,
        ref: str,
        pin_count: int,
        density: float,
    ) -> str:
        """Generate a description for a bottleneck component."""
        if density >= self.VERY_HIGH_DENSITY_THRESHOLD:
            severity = "Very high"
        else:
            severity = "High"

        # Guess package type from pin count
        if pin_count >= 100:
            pkg_type = "BGA"
        elif pin_count >= 32:
            pkg_type = "QFP"
        elif pin_count >= 16:
            pkg_type = "SOIC/SSOP"
        else:
            pkg_type = "IC"

        return f"{severity} pin density, {pin_count}-pin {pkg_type}, limited escape routing"
