"""Routing types for MCP tools.

Provides dataclasses for net routing status and operations.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NetRoutingStatus:
    """Routing status for a single net.

    Attributes:
        name: Net name (e.g., "GND", "SPI_CLK")
        status: Routing status ("unrouted", "partial", "complete")
        pins: Number of pads/pins on this net
        routed_connections: Number of connections already routed
        total_connections: Total number of connections needed (pins - 1 for tree)
        estimated_length_mm: Estimated routing length in millimeters
        difficulty: Estimated routing difficulty ("easy", "medium", "hard")
        reason: Explanation of difficulty rating if not easy
    """

    name: str
    status: str  # "unrouted", "partial", "complete"
    pins: int
    routed_connections: int
    total_connections: int
    estimated_length_mm: float
    difficulty: str  # "easy", "medium", "hard"
    reason: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "status": self.status,
            "pins": self.pins,
            "routed_connections": self.routed_connections,
            "total_connections": self.total_connections,
            "estimated_length_mm": round(self.estimated_length_mm, 2),
            "difficulty": self.difficulty,
            "reason": self.reason,
        }


@dataclass
class UnroutedNetsResult:
    """Result of get_unrouted_nets operation.

    Attributes:
        total_nets: Total number of nets in the design
        unrouted_count: Number of completely unrouted nets
        partial_count: Number of partially routed nets
        complete_count: Number of fully routed nets
        nets: List of nets needing routing (unrouted and partial)
    """

    total_nets: int
    unrouted_count: int
    partial_count: int
    complete_count: int
    nets: list[NetRoutingStatus] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "total_nets": self.total_nets,
            "unrouted_count": self.unrouted_count,
            "partial_count": self.partial_count,
            "complete_count": self.complete_count,
            "nets": [n.to_dict() for n in self.nets],
        }


@dataclass
class RouteNetResult:
    """Result of route_net operation.

    Attributes:
        success: Whether the routing operation succeeded
        net_name: Name of the net that was routed
        routed_connections: Number of connections successfully routed
        total_connections: Total connections that needed routing
        trace_length_mm: Total trace length in millimeters
        vias_used: Number of vias placed
        layers_used: List of layer names used for routing
        output_path: Path where the result was saved
        error_message: Error message if success is False
        suggestions: Suggestions if routing failed or was incomplete
    """

    success: bool
    net_name: str
    routed_connections: int = 0
    total_connections: int = 0
    trace_length_mm: float = 0.0
    vias_used: int = 0
    layers_used: list[str] = field(default_factory=list)
    output_path: str | None = None
    error_message: str | None = None
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "net_name": self.net_name,
            "routed_connections": self.routed_connections,
            "total_connections": self.total_connections,
            "trace_length_mm": round(self.trace_length_mm, 2),
            "vias_used": self.vias_used,
            "layers_used": self.layers_used,
            "output_path": self.output_path,
            "error_message": self.error_message,
            "suggestions": self.suggestions,
        }
