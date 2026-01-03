"""
Keep-out zone management for placement optimization.

Provides comprehensive keepout zone support including:
- Zone type definitions (mechanical, thermal, RF, assembly, clearance)
- Zone creation helpers for common scenarios
- Auto-detection from board features (mounting holes, connectors, edges)
- Violation detection and reporting

Example usage::

    from kicad_tools.optim.keepout import (
        KeepoutZone,
        KeepoutType,
        detect_keepout_zones,
        create_keepout_from_board_edge,
        validate_keepout_violations,
    )
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load("board.kicad_pcb")

    # Auto-detect keepout zones from board features
    zones = detect_keepout_zones(pcb)

    # Add edge clearance zone
    zones.append(create_keepout_from_board_edge(2.0, pcb))

    # Check for violations
    violations = validate_keepout_violations(pcb, zones)
    for v in violations:
        print(f"{v.component_ref} violates {v.zone_name}: {v.message}")
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import yaml

from kicad_tools.optim.components import Keepout
from kicad_tools.optim.geometry import Polygon, Vector2D

if TYPE_CHECKING:
    from kicad_tools.optim.placement import PlacementOptimizer
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "KeepoutType",
    "KeepoutZone",
    "KeepoutViolation",
    "create_keepout_from_component",
    "create_keepout_from_mounting_hole",
    "create_keepout_from_board_edge",
    "create_keepout_polygon",
    "detect_keepout_zones",
    "add_keepout_zones",
    "validate_keepout_violations",
    "load_keepout_zones_from_yaml",
]


class KeepoutType(Enum):
    """Type of keep-out zone."""

    MECHANICAL = "mechanical"  # Physical obstructions (connectors, enclosure)
    THERMAL = "thermal"  # Heat-sensitive areas (near hot components)
    RF = "rf"  # RF/antenna clearance zones
    ASSEMBLY = "assembly"  # Manufacturing constraints (pick-and-place)
    CLEARANCE = "clearance"  # General component clearance


@dataclass
class KeepoutZone:
    """
    A keep-out zone where components should not be placed.

    Supports various zone types with configurable clearances and
    layer-specific restrictions.
    """

    name: str
    zone_type: KeepoutType
    polygon: list[tuple[float, float]]  # Outline vertices in mm
    layer: str | None = None  # "F.Cu", "B.Cu", or None for all layers
    clearance_mm: float = 0.0  # Additional clearance around zone boundary
    allow_vias: bool = False  # If True, vias may pass through
    allow_traces: bool = False  # If True, traces may pass through
    charge_multiplier: float = 10.0  # Repulsion strength for optimizer

    def get_polygon(self) -> Polygon:
        """Get zone boundary as a Polygon object."""
        vertices = [Vector2D(x, y) for x, y in self.polygon]
        return Polygon(vertices=vertices)

    def get_expanded_polygon(self) -> Polygon:
        """Get zone boundary expanded by clearance as a Polygon object."""
        if self.clearance_mm <= 0:
            return self.get_polygon()

        # Simple expansion by moving vertices outward from centroid
        # More accurate expansion would use offset polygons
        vertices = [Vector2D(x, y) for x, y in self.polygon]
        if not vertices:
            return Polygon(vertices=[])

        # Find centroid
        cx = sum(v.x for v in vertices) / len(vertices)
        cy = sum(v.y for v in vertices) / len(vertices)
        centroid = Vector2D(cx, cy)

        # Expand each vertex outward from centroid
        expanded = []
        for v in vertices:
            direction = v - centroid
            dist = direction.magnitude()
            if dist > 1e-6:
                # Scale outward by clearance
                scale = (dist + self.clearance_mm) / dist
                expanded.append(centroid + direction * scale)
            else:
                expanded.append(v)

        return Polygon(vertices=expanded)

    def contains_point(self, x: float, y: float) -> bool:
        """Check if point is inside the zone (including clearance)."""
        poly = self.get_expanded_polygon()
        return poly.contains_point(Vector2D(x, y))

    def to_keepout(self) -> Keepout:
        """Convert to optimizer Keepout object."""
        return Keepout(
            outline=self.get_expanded_polygon(),
            charge_multiplier=self.charge_multiplier,
            name=self.name,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "type": self.zone_type.value,
            "polygon": self.polygon,
            "layer": self.layer,
            "clearance_mm": self.clearance_mm,
            "allow_vias": self.allow_vias,
            "allow_traces": self.allow_traces,
        }

    @classmethod
    def from_dict(cls, data: dict) -> KeepoutZone:
        """Create from dictionary."""
        zone_type = KeepoutType(data.get("type", "clearance"))
        return cls(
            name=data.get("name", ""),
            zone_type=zone_type,
            polygon=data.get("polygon", []),
            layer=data.get("layer"),
            clearance_mm=data.get("clearance_mm", 0.0),
            allow_vias=data.get("allow_vias", False),
            allow_traces=data.get("allow_traces", False),
            charge_multiplier=data.get("charge_multiplier", 10.0),
        )


@dataclass
class KeepoutViolation:
    """A component placement that violates a keep-out zone."""

    component_ref: str
    zone_name: str
    zone_type: KeepoutType
    position: tuple[float, float]
    overlap_mm: float  # How far component extends into zone
    message: str = ""

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "component": self.component_ref,
            "zone": self.zone_name,
            "type": self.zone_type.value,
            "position": self.position,
            "overlap_mm": self.overlap_mm,
            "message": self.message,
        }


def create_keepout_from_component(
    pcb: PCB,
    ref: str,
    clearance_mm: float = 1.0,
    zone_type: KeepoutType = KeepoutType.MECHANICAL,
) -> KeepoutZone | None:
    """
    Create a keepout zone around a component's courtyard.

    Uses the component's pad positions to estimate a bounding box,
    then adds the specified clearance.

    Args:
        pcb: Loaded PCB object
        ref: Component reference designator (e.g., "J1")
        clearance_mm: Additional clearance around component
        zone_type: Type of keepout zone

    Returns:
        KeepoutZone or None if component not found
    """
    # Find footprint by reference
    footprint = None
    for fp in pcb.footprints:
        if fp.reference == ref:
            footprint = fp
            break

    if footprint is None:
        return None

    # Compute bounding box from pads
    if not footprint.pads:
        # No pads, use small default area
        cx, cy = footprint.position
        hw, hh = 1.0, 1.0
    else:
        # Find bounds from pad positions and sizes
        min_x = min_y = float("inf")
        max_x = max_y = float("-inf")

        for pad in footprint.pads:
            px, py = pad.position
            pw, ph = pad.size
            min_x = min(min_x, px - pw / 2)
            max_x = max(max_x, px + pw / 2)
            min_y = min(min_y, py - ph / 2)
            max_y = max(max_y, py + ph / 2)

        # Transform to absolute coordinates
        fx, fy = footprint.position
        rot = math.radians(footprint.rotation)
        cos_r, sin_r = math.cos(rot), math.sin(rot)

        # Compute corners in component-local space
        corners = [
            (min_x, min_y),
            (max_x, min_y),
            (max_x, max_y),
            (min_x, max_y),
        ]

        # Transform to absolute and find bounding box
        abs_min_x = abs_min_y = float("inf")
        abs_max_x = abs_max_y = float("-inf")

        for lx, ly in corners:
            ax = fx + lx * cos_r - ly * sin_r
            ay = fy + lx * sin_r + ly * cos_r
            abs_min_x = min(abs_min_x, ax)
            abs_max_x = max(abs_max_x, ax)
            abs_min_y = min(abs_min_y, ay)
            abs_max_y = max(abs_max_y, ay)

        cx = (abs_min_x + abs_max_x) / 2
        cy = (abs_min_y + abs_max_y) / 2
        hw = (abs_max_x - abs_min_x) / 2
        hh = (abs_max_y - abs_min_y) / 2

    # Create rectangular polygon with clearance
    polygon = [
        (cx - hw - clearance_mm, cy - hh - clearance_mm),
        (cx + hw + clearance_mm, cy - hh - clearance_mm),
        (cx + hw + clearance_mm, cy + hh + clearance_mm),
        (cx - hw - clearance_mm, cy + hh + clearance_mm),
    ]

    return KeepoutZone(
        name=f"{ref}_keepout",
        zone_type=zone_type,
        polygon=polygon,
        layer=footprint.layer,
        clearance_mm=0.0,  # Already incorporated
    )


def create_keepout_from_mounting_hole(
    pcb: PCB,
    ref: str,
    clearance_mm: float = 2.0,
) -> KeepoutZone | None:
    """
    Create a circular keepout zone around a mounting hole.

    Args:
        pcb: Loaded PCB object
        ref: Mounting hole reference (e.g., "H1", "MH1")
        clearance_mm: Clearance radius from hole center

    Returns:
        KeepoutZone or None if mounting hole not found
    """
    # Find footprint by reference
    footprint = None
    for fp in pcb.footprints:
        if fp.reference == ref:
            footprint = fp
            break

    if footprint is None:
        return None

    cx, cy = footprint.position

    # Find the largest drill size for the hole radius
    hole_radius = 1.5  # Default
    for pad in footprint.pads:
        if pad.drill and pad.drill > 0:
            hole_radius = max(hole_radius, pad.drill / 2)

    # Create circular polygon (approximated with segments)
    radius = hole_radius + clearance_mm
    segments = 16
    polygon = []
    for i in range(segments):
        angle = 2 * math.pi * i / segments
        px = cx + radius * math.cos(angle)
        py = cy + radius * math.sin(angle)
        polygon.append((px, py))

    return KeepoutZone(
        name=f"{ref}_keepout",
        zone_type=KeepoutType.MECHANICAL,
        polygon=polygon,
        layer=None,  # Mounting holes affect all layers
        clearance_mm=0.0,  # Already incorporated
    )


def create_keepout_from_board_edge(
    distance_mm: float,
    pcb: PCB | None = None,
    board_outline: Polygon | None = None,
) -> KeepoutZone | None:
    """
    Create a keepout zone along board edges.

    Creates a zone that defines the valid placement area (interior),
    with the specified distance from edges as the exclusion zone.

    Args:
        distance_mm: Minimum distance from board edges
        pcb: PCB object (used to extract board outline)
        board_outline: Optional pre-extracted board outline

    Returns:
        KeepoutZone representing the edge exclusion, or None if no outline
    """
    if board_outline is None and pcb is None:
        return None

    outline = board_outline
    if outline is None and pcb is not None:
        # Try to extract from PCB Edge.Cuts
        outline = _extract_board_outline(pcb)

    if outline is None:
        return None

    # Create an inset polygon representing the "safe" area
    # The keepout zone is the area outside this inset
    # For simplicity, we invert - the keepout is a border zone around edges
    # We'll represent this as four rectangular strips along each edge

    # Get bounding box
    min_x = min(v.x for v in outline.vertices)
    max_x = max(v.x for v in outline.vertices)
    min_y = min(v.y for v in outline.vertices)
    max_y = max(v.y for v in outline.vertices)

    # For a simple rectangular board, create an outer frame
    # The "polygon" will be the outer edge minus an inset rectangle
    # But our polygon representation only supports solid regions

    # Instead, create a single "edge clearance" zone that uses
    # the inverted board outline expanded outward
    # Components should stay INSIDE the board minus clearance

    # Create inner boundary polygon (where components CAN go)
    inner_polygon = [
        (min_x + distance_mm, min_y + distance_mm),
        (max_x - distance_mm, min_y + distance_mm),
        (max_x - distance_mm, max_y - distance_mm),
        (min_x + distance_mm, max_y - distance_mm),
    ]

    # The keepout is everything outside this inner boundary
    # We represent this as the board outline with negative semantics
    # For the optimizer, we need the boundary itself as the repulsion source

    return KeepoutZone(
        name="board_edge_clearance",
        zone_type=KeepoutType.CLEARANCE,
        polygon=inner_polygon,
        layer=None,
        clearance_mm=0.0,
        # Special: this zone is an "allowed area" constraint
        # The optimizer interprets this differently
    )


def create_keepout_polygon(
    vertices: list[tuple[float, float]],
    zone_type: KeepoutType,
    name: str = "",
    clearance_mm: float = 0.0,
    layer: str | None = None,
) -> KeepoutZone:
    """
    Create a custom keepout zone from polygon vertices.

    Args:
        vertices: List of (x, y) tuples defining the polygon
        zone_type: Type of keepout zone
        name: Optional name for the zone
        clearance_mm: Additional clearance around zone boundary
        layer: Layer restriction ("F.Cu", "B.Cu", or None for all)

    Returns:
        KeepoutZone with the specified parameters
    """
    return KeepoutZone(
        name=name or f"custom_{zone_type.value}",
        zone_type=zone_type,
        polygon=list(vertices),
        layer=layer,
        clearance_mm=clearance_mm,
    )


def detect_keepout_zones(pcb: PCB) -> list[KeepoutZone]:
    """
    Auto-detect keep-out zones from board features.

    Detection sources:
    - Mounting holes (H*, MH*) → circular mechanical keepout
    - Large connectors (J*) → rectangular keepout on component side
    - Existing KiCad zones on Keepout layers

    Args:
        pcb: Loaded PCB object

    Returns:
        List of detected KeepoutZone objects
    """
    zones: list[KeepoutZone] = []

    for fp in pcb.footprints:
        ref_prefix = "".join(c for c in fp.reference if c.isalpha())

        # Mounting holes
        if ref_prefix in ("H", "MH"):
            zone = create_keepout_from_mounting_hole(pcb, fp.reference, clearance_mm=2.0)
            if zone:
                zones.append(zone)

        # Large connectors (estimate size from pad count and positions)
        elif ref_prefix == "J":
            # Check if it's a "large" connector (many pads or large area)
            if len(fp.pads) >= 4:
                zone = create_keepout_from_component(
                    pcb, fp.reference, clearance_mm=1.5, zone_type=KeepoutType.MECHANICAL
                )
                if zone:
                    zones.append(zone)

    # Detect existing KiCad keepout zones
    kicad_zones = _detect_kicad_keepout_zones(pcb)
    zones.extend(kicad_zones)

    return zones


def _detect_kicad_keepout_zones(pcb: PCB) -> list[KeepoutZone]:
    """Detect existing KiCad zones marked as keepout."""
    zones = []

    # Access raw S-expression to find zone elements with keepout properties
    sexp = pcb._sexp

    for child in sexp.iter_children():
        if child.tag != "zone":
            continue

        # Check for keepout properties
        is_keepout = False
        zone_name = ""

        if name_node := child.find("name"):
            zone_name = name_node.get_string(0) or ""

        # KiCad 8 uses (keepout ...) node with specific properties
        if child.find("keepout"):
            is_keepout = True

        # Also check for layers containing "Keepout"
        if layers_node := child.find("layers"):
            layer_strs = [layers_node.get_string(i) for i in range(10)]
            if any("keepout" in (s or "").lower() for s in layer_strs if s):
                is_keepout = True

        if not is_keepout:
            continue

        # Extract polygon vertices
        polygon_vertices = []
        if polygon_node := child.find("polygon"):
            if pts_node := polygon_node.find("pts"):
                for xy in pts_node.find_all("xy"):
                    x = xy.get_float(0) or 0.0
                    y = xy.get_float(1) or 0.0
                    polygon_vertices.append((x, y))

        if polygon_vertices:
            zones.append(
                KeepoutZone(
                    name=zone_name or f"kicad_keepout_{len(zones)}",
                    zone_type=KeepoutType.CLEARANCE,
                    polygon=polygon_vertices,
                    layer=None,
                    clearance_mm=0.0,
                )
            )

    return zones


def _extract_board_outline(pcb: PCB) -> Polygon | None:
    """Extract board outline from Edge.Cuts layer."""
    sexp = pcb._sexp

    # Look for gr_rect on Edge.Cuts
    for child in sexp.iter_children():
        if child.tag == "gr_rect":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                start = child.find("start")
                end = child.find("end")
                if start and end:
                    x1 = start.get_float(0) or 0.0
                    y1 = start.get_float(1) or 0.0
                    x2 = end.get_float(0) or 0.0
                    y2 = end.get_float(1) or 0.0
                    return Polygon(
                        vertices=[
                            Vector2D(x1, y1),
                            Vector2D(x2, y1),
                            Vector2D(x2, y2),
                            Vector2D(x1, y2),
                        ]
                    )

    # Look for gr_line elements on Edge.Cuts
    edge_lines: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for child in sexp.iter_children():
        if child.tag == "gr_line":
            layer = child.find("layer")
            if layer and layer.get_string(0) == "Edge.Cuts":
                start = child.find("start")
                end = child.find("end")
                if start and end:
                    x1 = start.get_float(0) or 0.0
                    y1 = start.get_float(1) or 0.0
                    x2 = end.get_float(0) or 0.0
                    y2 = end.get_float(1) or 0.0
                    edge_lines.append(((x1, y1), (x2, y2)))

    if len(edge_lines) >= 4:
        # Return bounding box
        all_x = [p[0] for line in edge_lines for p in line]
        all_y = [p[1] for line in edge_lines for p in line]
        return Polygon(
            vertices=[
                Vector2D(min(all_x), min(all_y)),
                Vector2D(max(all_x), min(all_y)),
                Vector2D(max(all_x), max(all_y)),
                Vector2D(min(all_x), max(all_y)),
            ]
        )

    return None


def add_keepout_zones(optimizer: PlacementOptimizer, zones: list[KeepoutZone]) -> int:
    """
    Add keep-out zones to the placement optimizer.

    Converts KeepoutZone objects to optimizer Keepout objects
    and adds them to the simulation.

    Args:
        optimizer: PlacementOptimizer instance
        zones: List of KeepoutZone objects

    Returns:
        Number of zones added
    """
    count = 0
    for zone in zones:
        keepout = zone.to_keepout()
        optimizer.keepouts.append(keepout)
        count += 1
    return count


def validate_keepout_violations(
    pcb: PCB,
    zones: list[KeepoutZone],
) -> list[KeepoutViolation]:
    """
    Check for components violating keep-out zones.

    Args:
        pcb: Loaded PCB object
        zones: List of KeepoutZone objects to check

    Returns:
        List of KeepoutViolation objects
    """
    violations: list[KeepoutViolation] = []

    for fp in pcb.footprints:
        cx, cy = fp.position

        # Check if component center is in any zone
        for zone in zones:
            if zone.contains_point(cx, cy):
                # Calculate approximate overlap
                poly = zone.get_expanded_polygon()
                centroid = poly.centroid()
                dist_to_center = math.sqrt((cx - centroid.x) ** 2 + (cy - centroid.y) ** 2)

                violations.append(
                    KeepoutViolation(
                        component_ref=fp.reference,
                        zone_name=zone.name,
                        zone_type=zone.zone_type,
                        position=(cx, cy),
                        overlap_mm=dist_to_center,
                        message=f"{fp.reference} at ({cx:.2f}, {cy:.2f}) is inside {zone.name} ({zone.zone_type.value} zone)",
                    )
                )

    return violations


def load_keepout_zones_from_yaml(yaml_path: str) -> list[KeepoutZone]:
    """
    Load keep-out zones from a YAML configuration file.

    Expected format:
    ```yaml
    keepouts:
      - name: usb_clearance
        type: mechanical
        polygon: [[0, 0], [10, 0], [10, 5], [0, 5]]
        clearance_mm: 1.0
      - name: antenna_zone
        type: rf
        polygon: [[50, 0], [60, 0], [60, 20], [50, 20]]
        clearance_mm: 5.0
    ```

    Args:
        yaml_path: Path to YAML file

    Returns:
        List of KeepoutZone objects
    """
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    zones = []
    for zone_data in data.get("keepouts", []):
        zones.append(KeepoutZone.from_dict(zone_data))

    return zones
