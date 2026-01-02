"""
Geometry primitives for optimization algorithms.

Provides 2D vector and polygon classes used throughout the optimization module
for physics calculations, component outlines, and board boundaries.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass, field

__all__ = ["Vector2D", "Polygon"]


@dataclass
class Vector2D:
    """2D vector for physics calculations."""

    x: float = 0.0
    y: float = 0.0

    def __add__(self, other: Vector2D) -> Vector2D:
        return Vector2D(self.x + other.x, self.y + other.y)

    def __sub__(self, other: Vector2D) -> Vector2D:
        return Vector2D(self.x - other.x, self.y - other.y)

    def __mul__(self, scalar: float) -> Vector2D:
        return Vector2D(self.x * scalar, self.y * scalar)

    def __rmul__(self, scalar: float) -> Vector2D:
        return self.__mul__(scalar)

    def __truediv__(self, scalar: float) -> Vector2D:
        return Vector2D(self.x / scalar, self.y / scalar)

    def __neg__(self) -> Vector2D:
        return Vector2D(-self.x, -self.y)

    def dot(self, other: Vector2D) -> float:
        """Dot product."""
        return self.x * other.x + self.y * other.y

    def cross(self, other: Vector2D) -> float:
        """2D cross product (returns scalar z-component)."""
        return self.x * other.y - self.y * other.x

    def magnitude(self) -> float:
        """Vector length."""
        return math.sqrt(self.x * self.x + self.y * self.y)

    def magnitude_squared(self) -> float:
        """Squared magnitude (faster, avoids sqrt)."""
        return self.x * self.x + self.y * self.y

    def normalized(self) -> Vector2D:
        """Unit vector in same direction."""
        mag = self.magnitude()
        if mag < 1e-10:
            return Vector2D(0.0, 0.0)
        return self / mag

    def rotated(self, angle_deg: float) -> Vector2D:
        """Rotate vector by angle in degrees."""
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        return Vector2D(
            self.x * cos_a - self.y * sin_a,
            self.x * sin_a + self.y * cos_a,
        )

    def perpendicular(self) -> Vector2D:
        """Return perpendicular vector (90 deg CCW)."""
        return Vector2D(-self.y, self.x)


@dataclass
class Polygon:
    """
    Closed polygon represented as a list of vertices.

    Used for board outlines and component bounding boxes.
    Vertices should be in counter-clockwise order for outward-facing normals.
    """

    vertices: list[Vector2D] = field(default_factory=list)

    @classmethod
    def rectangle(cls, x: float, y: float, width: float, height: float) -> Polygon:
        """Create a rectangle centered at (x, y)."""
        hw, hh = width / 2, height / 2
        return cls(
            vertices=[
                Vector2D(x - hw, y - hh),
                Vector2D(x + hw, y - hh),
                Vector2D(x + hw, y + hh),
                Vector2D(x - hw, y + hh),
            ]
        )

    @classmethod
    def circle(cls, x: float, y: float, radius: float, segments: int = 16) -> Polygon:
        """Create a circle approximated as a polygon."""
        vertices = []
        for i in range(segments):
            angle = 2 * math.pi * i / segments
            vx = x + radius * math.cos(angle)
            vy = y + radius * math.sin(angle)
            vertices.append(Vector2D(vx, vy))
        return cls(vertices=vertices)

    @classmethod
    def from_footprint_bounds(
        cls, x: float, y: float, width: float, height: float, rotation: float = 0.0
    ) -> Polygon:
        """Create rotated rectangle for a component footprint."""
        # Create centered rectangle
        hw, hh = width / 2, height / 2
        corners = [
            Vector2D(-hw, -hh),
            Vector2D(hw, -hh),
            Vector2D(hw, hh),
            Vector2D(-hw, hh),
        ]
        # Rotate and translate
        return cls(vertices=[v.rotated(rotation) + Vector2D(x, y) for v in corners])

    def edges(self) -> Iterator[tuple[Vector2D, Vector2D]]:
        """Iterate over edges as (start, end) pairs."""
        n = len(self.vertices)
        for i in range(n):
            yield self.vertices[i], self.vertices[(i + 1) % n]

    def centroid(self) -> Vector2D:
        """Compute polygon centroid."""
        if not self.vertices:
            return Vector2D(0.0, 0.0)
        cx = sum(v.x for v in self.vertices) / len(self.vertices)
        cy = sum(v.y for v in self.vertices) / len(self.vertices)
        return Vector2D(cx, cy)

    def area(self) -> float:
        """Compute signed area (positive for CCW vertices)."""
        n = len(self.vertices)
        if n < 3:
            return 0.0
        area = 0.0
        for i in range(n):
            j = (i + 1) % n
            area += self.vertices[i].x * self.vertices[j].y
            area -= self.vertices[j].x * self.vertices[i].y
        return area / 2

    def perimeter(self) -> float:
        """Compute polygon perimeter."""
        total = 0.0
        for v1, v2 in self.edges():
            total += (v2 - v1).magnitude()
        return total

    def contains_point(self, p: Vector2D) -> bool:
        """Test if point is inside polygon (ray casting)."""
        n = len(self.vertices)
        inside = False
        j = n - 1
        for i in range(n):
            vi, vj = self.vertices[i], self.vertices[j]
            if ((vi.y > p.y) != (vj.y > p.y)) and (
                p.x < (vj.x - vi.x) * (p.y - vi.y) / (vj.y - vi.y) + vi.x
            ):
                inside = not inside
            j = i
        return inside

    def translate(self, delta: Vector2D) -> Polygon:
        """Return translated polygon."""
        return Polygon(vertices=[v + delta for v in self.vertices])

    def rotate_around(self, center: Vector2D, angle_deg: float) -> Polygon:
        """Return polygon rotated around a center point."""
        return Polygon(vertices=[(v - center).rotated(angle_deg) + center for v in self.vertices])
