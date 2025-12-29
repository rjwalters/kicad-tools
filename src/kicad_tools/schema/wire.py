"""
Wire and junction models.

Represents electrical connections in a schematic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from ..core.sexp import SExp


@dataclass
class Wire:
    """
    A wire segment connecting two points.

    Wires carry electrical signals between pins, labels, and junctions.
    """

    start: Tuple[float, float]
    end: Tuple[float, float]
    uuid: str = ""
    stroke_width: float = 0
    stroke_type: str = "default"

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Wire:
        """Parse from S-expression."""
        start = (0.0, 0.0)
        end = (0.0, 0.0)
        uuid = ""
        stroke_width = 0.0
        stroke_type = "default"

        if pts := sexp.find("pts"):
            xy_nodes = pts.find_all("xy")
            if len(xy_nodes) >= 2:
                start = (xy_nodes[0].get_float(0) or 0, xy_nodes[0].get_float(1) or 0)
                end = (xy_nodes[1].get_float(0) or 0, xy_nodes[1].get_float(1) or 0)

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        if stroke := sexp.find("stroke"):
            if w := stroke.find("width"):
                stroke_width = w.get_float(0) or 0
            if t := stroke.find("type"):
                stroke_type = t.get_string(0) or "default"

        return cls(
            start=start,
            end=end,
            uuid=uuid,
            stroke_width=stroke_width,
            stroke_type=stroke_type,
        )

    @property
    def length(self) -> float:
        """Calculate the wire length."""
        dx = self.end[0] - self.start[0]
        dy = self.end[1] - self.start[1]
        return (dx * dx + dy * dy) ** 0.5

    def contains_point(self, point: Tuple[float, float], tolerance: float = 0.1) -> bool:
        """Check if a point lies on this wire segment."""
        x, y = point
        x1, y1 = self.start
        x2, y2 = self.end

        # Check if point is within bounding box
        if not (min(x1, x2) - tolerance <= x <= max(x1, x2) + tolerance):
            return False
        if not (min(y1, y2) - tolerance <= y <= max(y1, y2) + tolerance):
            return False

        # Check distance from line
        length = self.length
        if length < tolerance:
            # Wire is basically a point
            return ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5 < tolerance

        # Calculate perpendicular distance
        dist = abs((y2 - y1) * x - (x2 - x1) * y + x2 * y1 - y2 * x1) / length
        return dist < tolerance

    def __repr__(self) -> str:
        return f"Wire({self.start} -> {self.end})"


@dataclass
class Junction:
    """
    A junction point where multiple wires connect.

    Junctions explicitly mark connection points between wires.
    """

    position: Tuple[float, float]
    uuid: str = ""
    diameter: float = 0

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Junction:
        """Parse from S-expression."""
        pos = (0.0, 0.0)
        uuid = ""
        diameter = 0.0

        if at := sexp.find("at"):
            pos = (at.get_float(0) or 0, at.get_float(1) or 0)

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        if d := sexp.find("diameter"):
            diameter = d.get_float(0) or 0

        return cls(position=pos, uuid=uuid, diameter=diameter)

    def __repr__(self) -> str:
        return f"Junction({self.position})"


@dataclass
class Bus:
    """
    A bus segment (multiple signals grouped together).
    """

    start: Tuple[float, float]
    end: Tuple[float, float]
    uuid: str = ""

    @classmethod
    def from_sexp(cls, sexp: SExp) -> Bus:
        """Parse from S-expression."""
        start = (0.0, 0.0)
        end = (0.0, 0.0)
        uuid = ""

        if pts := sexp.find("pts"):
            xy_nodes = pts.find_all("xy")
            if len(xy_nodes) >= 2:
                start = (xy_nodes[0].get_float(0) or 0, xy_nodes[0].get_float(1) or 0)
                end = (xy_nodes[1].get_float(0) or 0, xy_nodes[1].get_float(1) or 0)

        if uuid_node := sexp.find("uuid"):
            uuid = uuid_node.get_string(0) or ""

        return cls(start=start, end=end, uuid=uuid)
