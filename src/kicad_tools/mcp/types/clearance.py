"""Clearance measurement types for MCP tools.

Provides dataclasses for measuring and reporting clearances
between copper elements on a PCB.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClearanceMeasurement:
    """A single clearance measurement between two copper elements.

    Attributes:
        from_item: Reference of the first item (e.g., "U1-1", "Track-abc123")
        from_type: Type of the first item ("pad", "track", "via")
        to_item: Reference of the second item
        to_type: Type of the second item
        clearance_mm: Edge-to-edge clearance in millimeters
        location: (x, y) location where the minimum clearance was measured
        layer: PCB layer where the clearance was measured
    """

    from_item: str
    from_type: str
    to_item: str
    to_type: str
    clearance_mm: float
    location: tuple[float, float]
    layer: str

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "from_item": self.from_item,
            "from_type": self.from_type,
            "to_item": self.to_item,
            "to_type": self.to_type,
            "clearance_mm": round(self.clearance_mm, 4),
            "location": {"x": self.location[0], "y": self.location[1]},
            "layer": self.layer,
        }


@dataclass
class ClearanceResult:
    """Result of a clearance measurement between items.

    Attributes:
        item1: First item identifier (component ref or net name)
        item2: Second item identifier (component ref or net name)
        min_clearance_mm: Minimum clearance found between items
        location: (x, y) position where minimum clearance occurs
        layer: Layer where minimum clearance was found
        clearances: List of all individual clearance measurements
        passes_rules: Whether the clearance meets design rules
        required_clearance_mm: Required minimum clearance from design rules
    """

    item1: str
    item2: str
    min_clearance_mm: float
    location: tuple[float, float]
    layer: str
    clearances: list[ClearanceMeasurement] = field(default_factory=list)
    passes_rules: bool = True
    required_clearance_mm: float | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "item1": self.item1,
            "item2": self.item2,
            "min_clearance_mm": round(self.min_clearance_mm, 4),
            "location": {"x": self.location[0], "y": self.location[1]},
            "layer": self.layer,
            "clearances": [c.to_dict() for c in self.clearances],
            "passes_rules": self.passes_rules,
            "required_clearance_mm": self.required_clearance_mm,
        }

    def summary(self) -> str:
        """Generate a human-readable summary of the clearance result."""
        status = "PASSES" if self.passes_rules else "FAILS"
        summary = f"Clearance between {self.item1} and {self.item2}: {self.min_clearance_mm:.4f} mm [{status}]"
        if self.required_clearance_mm is not None:
            summary += f" (required: {self.required_clearance_mm:.4f} mm)"
        summary += (
            f"\nLocation: ({self.location[0]:.3f}, {self.location[1]:.3f}) mm on {self.layer}"
        )
        return summary
