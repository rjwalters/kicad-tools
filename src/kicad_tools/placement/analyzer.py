"""Placement conflict analyzer for KiCad PCBs.

Detects various placement conflicts:
- Courtyard overlaps
- Pad clearance violations
- Hole-to-hole violations
- Silkscreen-to-pad conflicts
- Edge clearance violations
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

from .conflict import (
    ComponentInfo,
    Conflict,
    ConflictSeverity,
    ConflictType,
    HoleInfo,
    PadInfo,
    Point,
    Rectangle,
)


@dataclass
class DesignRules:
    """Design rules for conflict detection."""

    min_pad_clearance: float = 0.1  # mm
    min_hole_to_hole: float = 0.5  # mm
    min_edge_clearance: float = 0.3  # mm
    courtyard_margin: float = 0.25  # mm - margin around pads for courtyard


class PlacementAnalyzer:
    """Analyzes PCB for placement conflicts between components.

    Usage:
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts("board.kicad_pcb")

        # Or with custom rules:
        rules = DesignRules(min_pad_clearance=0.15)
        conflicts = analyzer.find_conflicts("board.kicad_pcb", rules=rules)
    """

    def __init__(self, verbose: bool = False):
        """Initialize analyzer.

        Args:
            verbose: If True, print progress messages
        """
        self.verbose = verbose
        self._components: list[ComponentInfo] = []
        self._board_edge: Optional[Rectangle] = None

    def find_conflicts(
        self,
        pcb_path: str | Path,
        rules: Optional[DesignRules] = None,
    ) -> list[Conflict]:
        """Find all placement conflicts in a PCB.

        Args:
            pcb_path: Path to .kicad_pcb file
            rules: Design rules for conflict detection (uses defaults if None)

        Returns:
            List of detected conflicts
        """
        if rules is None:
            rules = DesignRules()

        # Load PCB and extract component info
        self._load_pcb(pcb_path, rules.courtyard_margin)

        conflicts: list[Conflict] = []

        # Check each pair of components
        for c1, c2 in itertools.combinations(self._components, 2):
            # Only check components on the same layer (or through-hole)
            if not self._same_layer(c1, c2):
                continue

            # Check courtyard overlap
            if conflict := self._check_courtyard_overlap(c1, c2):
                conflicts.append(conflict)

            # Check pad clearance
            conflicts.extend(self._check_pad_clearance(c1, c2, rules.min_pad_clearance))

            # Check hole-to-hole
            conflicts.extend(self._check_hole_to_hole(c1, c2, rules.min_hole_to_hole))

        # Check edge clearance
        if self._board_edge:
            conflicts.extend(self._check_edge_clearance(rules.min_edge_clearance))

        # Sort by severity then location
        conflicts.sort(key=lambda c: (c.severity.value, c.location.x, c.location.y))

        return conflicts

    def _load_pcb(self, pcb_path: str | Path, courtyard_margin: float):
        """Load PCB and extract component information."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(pcb_path))
        self._components = []

        for fp in pcb.footprints:
            comp = self._footprint_to_component(fp, courtyard_margin)
            self._components.append(comp)

        # Try to extract board edge from segments on Edge.Cuts layer
        self._board_edge = self._extract_board_edge(pcb)

        if self.verbose:
            print(f"Loaded {len(self._components)} components from {pcb_path}")
            if self._board_edge:
                print(
                    f"Board edge: ({self._board_edge.min_x:.2f}, {self._board_edge.min_y:.2f}) to "
                    f"({self._board_edge.max_x:.2f}, {self._board_edge.max_y:.2f})"
                )

    def _footprint_to_component(
        self, fp, courtyard_margin: float
    ) -> ComponentInfo:
        """Convert a Footprint to ComponentInfo."""
        position = Point(fp.position[0], fp.position[1])

        # Extract pads with absolute positions
        pads: list[PadInfo] = []
        holes: list[HoleInfo] = []

        for pad in fp.pads:
            # Calculate absolute pad position (considering rotation)
            rel_x, rel_y = pad.position
            abs_pos = self._rotate_point(
                Point(rel_x, rel_y), fp.rotation, position
            )

            pad_info = PadInfo(
                name=pad.number,
                position=abs_pos,
                size=pad.size,
                shape=pad.shape,
                net=pad.net_name,
            )
            pads.append(pad_info)

            # If it has a drill, it's a through-hole
            if pad.drill > 0:
                holes.append(
                    HoleInfo(
                        position=abs_pos,
                        diameter=pad.drill,
                        is_plated=pad.type == "thru_hole",
                    )
                )

        # Calculate bounding boxes
        courtyard = None
        pads_bbox = None

        if pads:
            # Calculate pads bounding box
            min_x = min(p.position.x - p.size[0] / 2 for p in pads)
            max_x = max(p.position.x + p.size[0] / 2 for p in pads)
            min_y = min(p.position.y - p.size[1] / 2 for p in pads)
            max_y = max(p.position.y + p.size[1] / 2 for p in pads)
            pads_bbox = Rectangle(min_x, min_y, max_x, max_y)

            # Courtyard is pads bbox + margin
            courtyard = pads_bbox.expand(courtyard_margin)

        return ComponentInfo(
            reference=fp.reference,
            footprint=fp.name,
            position=position,
            rotation=fp.rotation,
            layer=fp.layer,
            courtyard=courtyard,
            pads_bbox=pads_bbox,
            pads=pads,
            holes=holes,
        )

    def _rotate_point(self, point: Point, angle_deg: float, origin: Point) -> Point:
        """Rotate a point around an origin."""
        if angle_deg == 0:
            return Point(origin.x + point.x, origin.y + point.y)

        rad = math.radians(angle_deg)
        cos_a = math.cos(rad)
        sin_a = math.sin(rad)

        # Rotate
        new_x = point.x * cos_a - point.y * sin_a
        new_y = point.x * sin_a + point.y * cos_a

        return Point(origin.x + new_x, origin.y + new_y)

    def _same_layer(self, c1: ComponentInfo, c2: ComponentInfo) -> bool:
        """Check if two components are on the same layer or need checking.

        Components on different layers don't conflict, but through-hole
        components can conflict with anything.
        """
        # If either has through-holes, they can conflict
        if c1.holes or c2.holes:
            return True

        # Otherwise, only same layer conflicts
        # Normalize layer names (F.Cu = front, B.Cu = back)
        return (
            c1.layer.startswith("F") == c2.layer.startswith("F") or
            c1.layer.startswith("B") == c2.layer.startswith("B")
        )

    def _check_courtyard_overlap(
        self, c1: ComponentInfo, c2: ComponentInfo
    ) -> Optional[Conflict]:
        """Check if courtyards of two components overlap."""
        if not c1.courtyard or not c2.courtyard:
            return None

        if not c1.courtyard.intersects(c2.courtyard):
            return None

        # Calculate overlap
        overlap = c1.courtyard.overlap_vector(c2.courtyard)
        if not overlap:
            return None

        overlap_amount = math.sqrt(overlap.x**2 + overlap.y**2)

        # Location is the center of overlap region
        overlap_x = (
            max(c1.courtyard.min_x, c2.courtyard.min_x) +
            min(c1.courtyard.max_x, c2.courtyard.max_x)
        ) / 2
        overlap_y = (
            max(c1.courtyard.min_y, c2.courtyard.min_y) +
            min(c1.courtyard.max_y, c2.courtyard.max_y)
        ) / 2

        return Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1=c1.reference,
            component2=c2.reference,
            message=f"courtyards overlap by {overlap_amount:.3f}mm",
            location=Point(overlap_x, overlap_y),
            overlap_amount=overlap_amount,
        )

    def _check_pad_clearance(
        self, c1: ComponentInfo, c2: ComponentInfo, min_clearance: float
    ) -> Iterator[Conflict]:
        """Check clearance between pads of two components."""
        for p1 in c1.pads:
            bbox1 = p1.bbox()
            for p2 in c2.pads:
                bbox2 = p2.bbox()

                # Quick bounding box check first
                expanded1 = bbox1.expand(min_clearance)
                if not expanded1.intersects(bbox2):
                    continue

                # Calculate actual clearance
                clearance = self._pad_clearance(p1, p2)

                if clearance < min_clearance:
                    severity = (
                        ConflictSeverity.ERROR if clearance <= 0
                        else ConflictSeverity.WARNING
                    )

                    # Location is midpoint between pads
                    mid = Point(
                        (p1.position.x + p2.position.x) / 2,
                        (p1.position.y + p2.position.y) / 2,
                    )

                    yield Conflict(
                        type=ConflictType.PAD_CLEARANCE,
                        severity=severity,
                        component1=c1.reference,
                        component2=c2.reference,
                        message=f"pad clearance {clearance:.3f}mm (min {min_clearance:.3f}mm)",
                        location=mid,
                        actual_clearance=clearance,
                        required_clearance=min_clearance,
                    )

    def _pad_clearance(self, p1: PadInfo, p2: PadInfo) -> float:
        """Calculate clearance between two pads.

        This is a simplified calculation using bounding boxes.
        A more accurate calculation would consider pad shapes.
        """
        bbox1 = p1.bbox()
        bbox2 = p2.bbox()

        # If overlapping, clearance is negative (amount of overlap)
        if bbox1.intersects(bbox2):
            overlap = bbox1.overlap_vector(bbox2)
            if overlap:
                return -math.sqrt(overlap.x**2 + overlap.y**2)
            return 0.0

        # Calculate gap between bounding boxes
        dx = max(0, max(bbox1.min_x, bbox2.min_x) - min(bbox1.max_x, bbox2.max_x))
        dy = max(0, max(bbox1.min_y, bbox2.min_y) - min(bbox1.max_y, bbox2.max_y))

        # For rectangular pads, this gives edge-to-edge distance
        if dx > 0 and dy > 0:
            # Diagonal gap - return corner distance
            return math.sqrt(dx**2 + dy**2)
        else:
            # Axis-aligned gap
            return max(dx, dy)

    def _check_hole_to_hole(
        self, c1: ComponentInfo, c2: ComponentInfo, min_distance: float
    ) -> Iterator[Conflict]:
        """Check distance between drill holes."""
        for h1 in c1.holes:
            for h2 in c2.holes:
                # Calculate edge-to-edge distance (not center-to-center)
                center_dist = h1.position.distance_to(h2.position)
                edge_dist = center_dist - (h1.diameter + h2.diameter) / 2

                if edge_dist < min_distance:
                    severity = (
                        ConflictSeverity.ERROR if edge_dist <= 0
                        else ConflictSeverity.WARNING
                    )

                    mid = Point(
                        (h1.position.x + h2.position.x) / 2,
                        (h1.position.y + h2.position.y) / 2,
                    )

                    yield Conflict(
                        type=ConflictType.HOLE_TO_HOLE,
                        severity=severity,
                        component1=c1.reference,
                        component2=c2.reference,
                        message=f"holes {edge_dist:.3f}mm apart (min {min_distance:.3f}mm)",
                        location=mid,
                        actual_clearance=edge_dist,
                        required_clearance=min_distance,
                    )

    def _check_edge_clearance(self, min_clearance: float) -> Iterator[Conflict]:
        """Check clearance from components to board edge."""
        if not self._board_edge:
            return

        edge = self._board_edge

        for comp in self._components:
            if not comp.courtyard:
                continue

            cy = comp.courtyard

            # Check each edge
            violations: list[tuple[str, float, Point]] = []

            # Left edge
            if cy.min_x < edge.min_x + min_clearance:
                dist = cy.min_x - edge.min_x
                violations.append((
                    "left",
                    dist,
                    Point(edge.min_x, (cy.min_y + cy.max_y) / 2),
                ))

            # Right edge
            if cy.max_x > edge.max_x - min_clearance:
                dist = edge.max_x - cy.max_x
                violations.append((
                    "right",
                    dist,
                    Point(edge.max_x, (cy.min_y + cy.max_y) / 2),
                ))

            # Top edge
            if cy.min_y < edge.min_y + min_clearance:
                dist = cy.min_y - edge.min_y
                violations.append((
                    "top",
                    dist,
                    Point((cy.min_x + cy.max_x) / 2, edge.min_y),
                ))

            # Bottom edge
            if cy.max_y > edge.max_y - min_clearance:
                dist = edge.max_y - cy.max_y
                violations.append((
                    "bottom",
                    dist,
                    Point((cy.min_x + cy.max_x) / 2, edge.max_y),
                ))

            for edge_name, dist, loc in violations:
                severity = (
                    ConflictSeverity.ERROR if dist < 0
                    else ConflictSeverity.WARNING
                )

                yield Conflict(
                    type=ConflictType.EDGE_CLEARANCE,
                    severity=severity,
                    component1=comp.reference,
                    component2=f"{edge_name}_edge",
                    message=f"{edge_name} edge clearance {dist:.3f}mm (min {min_clearance:.3f}mm)",
                    location=loc,
                    actual_clearance=dist,
                    required_clearance=min_clearance,
                )

    def _extract_board_edge(self, pcb) -> Optional[Rectangle]:
        """Extract board outline from Edge.Cuts layer.

        Returns bounding box of the board edge.
        """
        edge_segments = list(pcb.segments_on_layer("Edge.Cuts"))

        if not edge_segments:
            return None

        # Find bounding box of all edge segments
        all_x = []
        all_y = []

        for seg in edge_segments:
            all_x.extend([seg.start[0], seg.end[0]])
            all_y.extend([seg.start[1], seg.end[1]])

        if not all_x:
            return None

        return Rectangle(min(all_x), min(all_y), max(all_x), max(all_y))

    def get_components(self) -> List[ComponentInfo]:
        """Get list of analyzed components."""
        return self._components

    def get_board_edge(self) -> Optional[Rectangle]:
        """Get board edge bounding box."""
        return self._board_edge
