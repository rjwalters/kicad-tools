"""Placement analysis types for MCP tools.

Provides dataclasses for analyzing component placement quality
including scores, issues, clusters, and routing estimates.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PlacementScores:
    """Placement quality scores by category.

    Attributes:
        wire_length: Wire length score (lower is better, 0-100 normalized).
        congestion: Congestion score (lower is better, 0-100 normalized).
        thermal: Thermal quality score (higher is better, proper heat spreading).
        signal_integrity: Signal integrity score (higher is better).
        manufacturing: Manufacturing/DFM score (higher is better).
    """

    wire_length: float
    congestion: float
    thermal: float
    signal_integrity: float
    manufacturing: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "wire_length": round(self.wire_length, 1),
            "congestion": round(self.congestion, 1),
            "thermal": round(self.thermal, 1),
            "signal_integrity": round(self.signal_integrity, 1),
            "manufacturing": round(self.manufacturing, 1),
        }


@dataclass
class PlacementIssue:
    """A placement issue or recommendation.

    Attributes:
        severity: Issue severity ("critical", "warning", "suggestion").
        category: Issue category ("thermal", "routing", "si", "dfm").
        description: Human-readable description of the issue.
        affected_components: List of component reference designators involved.
        suggestion: Actionable suggestion to fix the issue.
        location: Optional (x, y) location in mm.
    """

    severity: str
    category: str
    description: str
    affected_components: list[str]
    suggestion: str
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result: dict = {
            "severity": self.severity,
            "category": self.category,
            "description": self.description,
            "affected_components": self.affected_components,
            "suggestion": self.suggestion,
        }
        if self.location is not None:
            result["location"] = {"x": round(self.location[0], 2), "y": round(self.location[1], 2)}
        return result


@dataclass
class PlacementCluster:
    """A detected functional cluster of components.

    Attributes:
        name: Cluster name (e.g., "mcu_cluster", "power_section").
        components: List of component reference designators in the cluster.
        centroid: Cluster center position (x, y) in mm.
        compactness_score: How compact the cluster is (0-100, higher is better).
    """

    name: str
    components: list[str]
    centroid: tuple[float, float]
    compactness_score: float

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "components": self.components,
            "centroid": {"x": round(self.centroid[0], 2), "y": round(self.centroid[1], 2)},
            "compactness_score": round(self.compactness_score, 1),
        }


@dataclass
class RoutingEstimate:
    """Estimated routing difficulty based on placement.

    Attributes:
        estimated_routability: Routability score (0-100, higher is easier to route).
        congestion_hotspots: List of (x, y) positions with high congestion.
        difficult_nets: List of net names that will be difficult to route.
    """

    estimated_routability: float
    congestion_hotspots: list[tuple[float, float]] = field(default_factory=list)
    difficult_nets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "estimated_routability": round(self.estimated_routability, 1),
            "congestion_hotspots": [
                {"x": round(x, 2), "y": round(y, 2)} for x, y in self.congestion_hotspots
            ],
            "difficult_nets": self.difficult_nets,
        }


@dataclass
class PlacementAnalysis:
    """Complete placement quality analysis.

    This is the main result type returned by placement_analyze().
    Contains comprehensive information about placement quality including
    scores by category, identified issues, functional clusters, and
    routing difficulty estimate.

    Attributes:
        file_path: Absolute path to the analyzed PCB file.
        overall_score: Overall placement quality score (0-100).
        categories: Scores broken down by category.
        issues: List of identified placement issues.
        clusters: Detected functional clusters.
        routing_estimate: Estimated routing difficulty.
    """

    file_path: str
    overall_score: float
    categories: PlacementScores
    issues: list[PlacementIssue] = field(default_factory=list)
    clusters: list[PlacementCluster] = field(default_factory=list)
    routing_estimate: RoutingEstimate = field(
        default_factory=lambda: RoutingEstimate(estimated_routability=0.0)
    )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "file_path": self.file_path,
            "overall_score": round(self.overall_score, 1),
            "categories": self.categories.to_dict(),
            "issues": [i.to_dict() for i in self.issues],
            "clusters": [c.to_dict() for c in self.clusters],
            "routing_estimate": self.routing_estimate.to_dict(),
        }
