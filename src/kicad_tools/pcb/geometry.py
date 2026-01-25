"""
Geometry primitives for PCB block layout.

This module provides basic geometric types used by the PCB blocks system:
- Point: 2D point with rotation and arithmetic operations
- Rectangle: Axis-aligned bounding box
- Layer: PCB layer enumeration (imported from core.types)
"""

import math
from dataclasses import dataclass

from kicad_tools.core.types import Layer


@dataclass
class Point:
    """2D point in mm."""

    x: float
    y: float

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Point") -> "Point":
        return Point(self.x - other.x, self.y - other.y)

    def rotate(self, angle_deg: float, origin: "Point | None" = None) -> "Point":
        """Rotate point around origin (default: 0,0)."""
        if origin is None:
            origin = Point(0, 0)

        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)

        # Translate to origin
        dx = self.x - origin.x
        dy = self.y - origin.y

        # Rotate
        new_x = dx * cos_a - dy * sin_a
        new_y = dx * sin_a + dy * cos_a

        # Translate back
        return Point(new_x + origin.x, new_y + origin.y)

    def tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Rectangle:
    """Axis-aligned bounding box."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return Point((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def contains(self, p: Point) -> bool:
        return self.min_x <= p.x <= self.max_x and self.min_y <= p.y <= self.max_y

    def expand(self, margin: float) -> "Rectangle":
        """Return expanded rectangle."""
        return Rectangle(
            self.min_x - margin, self.min_y - margin, self.max_x + margin, self.max_y + margin
        )


__all__ = ["Point", "Rectangle", "Layer"]
