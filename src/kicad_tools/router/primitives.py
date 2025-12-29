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

import uuid
from dataclasses import dataclass, field
from typing import List, Tuple

from .layers import Layer


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

    def grid_key(self, resolution: float) -> Tuple[int, int, int]:
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


@dataclass
class Via:
    """A via connecting layers."""

    x: float
    y: float
    drill: float
    diameter: float
    layers: Tuple[Layer, Layer]
    net: int = 0
    net_name: str = ""

    def to_sexp(self) -> str:
        """Generate KiCad S-expression."""
        layer_start = self.layers[0].kicad_name
        layer_end = self.layers[1].kicad_name
        return f"""(via
\t\t(at {self.x:.4f} {self.y:.4f})
\t\t(size {self.diameter})
\t\t(drill {self.drill})
\t\t(layers "{layer_start}" "{layer_end}")
\t\t(net {self.net})
\t\t(uuid "{uuid.uuid4()}")
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

    def to_sexp(self) -> str:
        """Generate KiCad S-expression."""
        return f"""(segment
\t\t(start {self.x1:.4f} {self.y1:.4f})
\t\t(end {self.x2:.4f} {self.y2:.4f})
\t\t(width {self.width})
\t\t(layer "{self.layer.kicad_name}")
\t\t(net {self.net})
\t\t(uuid "{uuid.uuid4()}")
\t)"""


@dataclass
class Route:
    """A complete route between two points."""

    net: int
    net_name: str
    segments: List[Segment] = field(default_factory=list)
    vias: List[Via] = field(default_factory=list)

    def to_sexp(self) -> str:
        """Generate all S-expressions for this route."""
        parts = []
        for seg in self.segments:
            parts.append(seg.to_sexp())
        for via in self.vias:
            parts.append(via.to_sexp())
        return "\n\t".join(parts)


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
    through_hole: bool = False  # PTH pads block both layers
    drill: float = 0.0  # Drill diameter for PTH pads (0 = use pad size)


@dataclass
class Obstacle:
    """An obstacle to avoid."""

    x: float
    y: float
    width: float
    height: float
    layer: Layer
    clearance: float = 0.0
