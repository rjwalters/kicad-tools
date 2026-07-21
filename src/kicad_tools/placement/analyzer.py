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
import os
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

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
    # Maximum gap an off-board connector's courtyard may stand off from the
    # nearest board edge before it is flagged as marooned in the interior
    # (issue #4450). Board-03's USB-C J1 sits 8 mm inside the north edge.
    edge_connector_max_inset: float = 2.0  # mm


class PlacementAnalyzer:
    """Analyzes PCB for placement conflicts between components.

    Usage:
        analyzer = PlacementAnalyzer()
        conflicts = analyzer.find_conflicts("board.kicad_pcb")

        # Or with custom rules:
        rules = DesignRules(min_pad_clearance=0.15)
        conflicts = analyzer.find_conflicts("board.kicad_pcb", rules=rules)

    .. note::
        This analyzer powers ``kct placement check``. Its courtyard-overlap
        metric reads each footprint's **real** ``F.CrtYd`` / ``B.CrtYd``
        polygon (via the shared helpers in
        :mod:`kicad_tools.geometry.courtyard`, the same geometry ``kct
        check``'s ``CourtyardOverlapRule`` and KiCad's DRC use) and tests for a
        positive-area polygon intersection, so ``kct placement check`` and
        ``kct check`` agree on courtyard overlaps (issue #4182). For
        footprints with no resolvable courtyard artwork it falls back to the
        legacy approximation: the footprint's pad bounding box expanded by
        ``courtyard_margin`` (default 0.25 mm), tested as an axis-aligned
        rectangle overlap.

        This is a **different metric** from the optimizer objective in
        :func:`kicad_tools.placement.cost.evaluate_placement`, which uses raw
        axis-aligned bounding-box overlap area with no courtyard margin.
        Keeping the two separate is intentional (actionable diagnostics vs. a
        smooth optimizer search space); see ``docs/placement-scoring.md`` for
        the full comparison (issues #3940, #4182).
    """

    def __init__(self, verbose: bool = False):
        """Initialize analyzer.

        Args:
            verbose: If True, print progress messages
        """
        self.verbose = verbose
        self._components: list[ComponentInfo] = []
        self._board_edge: Rectangle | None = None
        # Sheet-absolute position of the board-relative frame's origin (the
        # PCB's detected board origin).  Used only to echo sheet coordinates
        # in off-board messages on offset boards (issue #4290).
        self._board_origin_offset: tuple[float, float] = (0.0, 0.0)

    def find_conflicts(
        self,
        pcb_path: str | Path,
        rules: DesignRules | None = None,
        max_workers: int | None = None,
    ) -> list[Conflict]:
        """Find all placement conflicts in a PCB.

        Args:
            pcb_path: Path to .kicad_pcb file
            rules: Design rules for conflict detection (uses defaults if None)
            max_workers: Maximum number of worker threads for parallel conflict
                detection. Defaults to CPU count. Set to 1 to disable parallelism.

        Returns:
            List of detected conflicts
        """
        if rules is None:
            rules = DesignRules()

        # Load PCB and extract component info
        self._load_pcb(pcb_path, rules.courtyard_margin)

        # Use CPU count if max_workers not specified
        if max_workers is None:
            max_workers = os.cpu_count() or 1

        # Generate component pairs that need checking
        pairs_to_check = [
            (c1, c2)
            for c1, c2 in itertools.combinations(self._components, 2)
            if self._same_layer(c1, c2)
        ]

        if self.verbose:
            print(f"Checking {len(pairs_to_check)} component pairs with {max_workers} workers")

        # Check pairs in parallel
        conflicts = self._check_pairs_parallel(pairs_to_check, rules, max_workers)

        # Check edge clearance (sequential, typically small)
        if self._board_edge:
            conflicts.extend(self._check_off_board())
            conflicts.extend(self._check_edge_clearance(rules.min_edge_clearance))
            conflicts.extend(self._check_edge_connectors(rules.edge_connector_max_inset))

        # Sort by severity then location
        conflicts.sort(key=lambda c: (c.severity.value, c.location.x, c.location.y))

        return conflicts

    def _check_pair(
        self,
        pair: tuple[ComponentInfo, ComponentInfo],
        rules: DesignRules,
    ) -> list[Conflict]:
        """Check a single component pair for all conflict types.

        This method is designed to be called in parallel - it only reads
        shared state and doesn't modify any instance variables.

        Args:
            pair: Tuple of two components to check
            rules: Design rules for conflict detection

        Returns:
            List of conflicts found between the two components
        """
        c1, c2 = pair
        conflicts: list[Conflict] = []

        # Check courtyard overlap
        if conflict := self._check_courtyard_overlap(c1, c2):
            conflicts.append(conflict)

        # Check pad clearance
        conflicts.extend(self._check_pad_clearance(c1, c2, rules.min_pad_clearance))

        # Check hole-to-hole
        conflicts.extend(self._check_hole_to_hole(c1, c2, rules.min_hole_to_hole))

        return conflicts

    def _check_pairs_parallel(
        self,
        pairs: list[tuple[ComponentInfo, ComponentInfo]],
        rules: DesignRules,
        max_workers: int,
    ) -> list[Conflict]:
        """Check multiple component pairs in parallel.

        Args:
            pairs: List of component pairs to check
            rules: Design rules for conflict detection
            max_workers: Maximum number of worker threads

        Returns:
            Flattened list of all conflicts found
        """
        # For small numbers of pairs or single worker, run sequentially
        if max_workers <= 1 or len(pairs) < 4:
            conflicts: list[Conflict] = []
            for pair in pairs:
                conflicts.extend(self._check_pair(pair, rules))
            return conflicts

        # Run in parallel using ThreadPoolExecutor
        all_conflicts: list[Conflict] = []

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Map each pair to its conflicts
            results = executor.map(lambda p: self._check_pair(p, rules), pairs)

            # Flatten results
            for pair_conflicts in results:
                all_conflicts.extend(pair_conflicts)

        return all_conflicts

    def _load_pcb(self, pcb_path: str | Path, courtyard_margin: float):
        """Load PCB and extract component information."""
        from kicad_tools.schema import PCB

        pcb = PCB.load(str(pcb_path))
        self._load_pcb_from_instance(pcb, courtyard_margin)

        if self.verbose:
            print(f"Loaded {len(self._components)} components from {pcb_path}")
            if self._board_edge:
                print(
                    f"Board edge: ({self._board_edge.min_x:.2f}, {self._board_edge.min_y:.2f}) to "
                    f"({self._board_edge.max_x:.2f}, {self._board_edge.max_y:.2f})"
                )

    def _load_pcb_from_instance(self, pcb, courtyard_margin: float):
        """Load component information from an existing PCB instance.

        This allows checking collisions on a PCB that's already loaded in memory,
        without having to save and reload from disk.

        Args:
            pcb: A PCB instance (from kicad_tools.schema)
            courtyard_margin: Margin to add around pads for courtyard calculation
        """
        self._components = []

        for fp in pcb.footprints:
            comp = self._footprint_to_component(fp, courtyard_margin)
            self._components.append(comp)

        # Try to extract board edge from segments on Edge.Cuts layer
        self._board_edge = self._extract_board_edge(pcb)

    def _find_conflicts_internal(self, rules: DesignRules) -> list[Conflict]:
        """Find conflicts using already-loaded component data.

        This is used by PCB methods that have already loaded components
        via _load_pcb_from_instance().

        Args:
            rules: Design rules for conflict detection

        Returns:
            List of detected conflicts
        """
        # Generate component pairs that need checking
        pairs_to_check = [
            (c1, c2)
            for c1, c2 in itertools.combinations(self._components, 2)
            if self._same_layer(c1, c2)
        ]

        # Check pairs (using single worker for internal use)
        conflicts = self._check_pairs_parallel(pairs_to_check, rules, max_workers=1)

        # Check edge clearance
        if self._board_edge:
            conflicts.extend(self._check_off_board())
            conflicts.extend(self._check_edge_clearance(rules.min_edge_clearance))
            conflicts.extend(self._check_edge_connectors(rules.edge_connector_max_inset))

        # Sort by severity then location
        conflicts.sort(key=lambda c: (c.severity.value, c.location.x, c.location.y))

        return conflicts

    def _footprint_to_component(self, fp, courtyard_margin: float) -> ComponentInfo:
        """Convert a Footprint to ComponentInfo."""
        position = Point(fp.position[0], fp.position[1])

        # Extract pads with absolute positions
        pads: list[PadInfo] = []
        holes: list[HoleInfo] = []

        for pad in fp.pads:
            # Calculate absolute pad position (considering rotation).
            # KiCad applies the footprint orientation as a NEGATED angle vs
            # standard CCW math (verified vs pcbnew, issue #3739); _rotate_point
            # is standard-CCW, so pass -rotation to match KiCad pad positions.
            rel_x, rel_y = pad.position
            abs_pos = self._rotate_point(Point(rel_x, rel_y), -fp.rotation, position)

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

            # Courtyard fallback: pads bbox + margin (used when the footprint
            # has no resolvable F.CrtYd/B.CrtYd geometry).
            courtyard = pads_bbox.expand(courtyard_margin)

        # Prefer the real courtyard polygon (issue #4182): read the actual
        # F.CrtYd / B.CrtYd graphics so `kct placement check` agrees with
        # `kct check` (and KiCad's DRC) on courtyard overlaps rather than using
        # the coarse pads-bbox approximation, which only ever finds a strict
        # subset.  Fall back to the pads-bbox+margin courtyard above when a
        # footprint has no resolvable courtyard artwork.
        courtyard_polygon = self._resolve_courtyard_polygon(fp)
        if courtyard_polygon is not None:
            min_x, min_y, max_x, max_y = courtyard_polygon.bounds
            courtyard = Rectangle(min_x, min_y, max_x, max_y)

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
            courtyard_polygon=courtyard_polygon,
        )

    def _resolve_courtyard_polygon(self, fp):
        """Build the footprint's real courtyard polygon, or None.

        Uses the shared courtyard-geometry helpers (also used by
        ``kct check``'s ``CourtyardOverlapRule``) to read the true F.CrtYd /
        B.CrtYd outline, honoring the footprint's position and rotation.  The
        component's own board side (F/B) selects which courtyard layer to read;
        a footprint carrying courtyards on both sides (tall/THT parts) uses the
        courtyard on the side it is placed on.  Returns ``None`` when shapely is
        unavailable or no courtyard outline can be resolved, so the caller falls
        back to the pads-bbox+margin approximation.
        """
        try:
            from kicad_tools._shapely import has_shapely
        except Exception:
            return None
        if not has_shapely():
            return None

        from shapely.geometry import Polygon  # type: ignore[import-untyped]

        from kicad_tools.geometry.courtyard import _courtyard_polygon, _side_has_geometry

        side = "B" if str(fp.layer).startswith("B") else "F"
        if not _side_has_geometry(fp, side):
            return None
        return _courtyard_polygon(fp, side, Polygon)

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
        return c1.layer.startswith("F") == c2.layer.startswith("F") or c1.layer.startswith(
            "B"
        ) == c2.layer.startswith("B")

    def _check_courtyard_overlap(self, c1: ComponentInfo, c2: ComponentInfo) -> Conflict | None:
        """Check if courtyards of two components overlap.

        When both components have a resolved real courtyard polygon (issue
        #4182), use a positive-area polygon intersection — the same test
        ``kct check``'s ``CourtyardOverlapRule`` and KiCad's DRC perform — so
        the two checkers agree.  Otherwise fall back to the coarse axis-aligned
        bounding-box test on the pads-bbox+margin courtyard.
        """
        if c1.courtyard_polygon is not None and c2.courtyard_polygon is not None:
            return self._check_courtyard_overlap_polygon(c1, c2)

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
            max(c1.courtyard.min_x, c2.courtyard.min_x)
            + min(c1.courtyard.max_x, c2.courtyard.max_x)
        ) / 2
        overlap_y = (
            max(c1.courtyard.min_y, c2.courtyard.min_y)
            + min(c1.courtyard.max_y, c2.courtyard.max_y)
        ) / 2

        return Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1=c1.reference,
            component2=c2.reference,
            message=(
                f"courtyards overlap by {overlap_amount:.3f}mm "
                "(pad-bbox fallback — no F.CrtYd artwork)"
            ),
            location=Point(overlap_x, overlap_y),
            overlap_amount=overlap_amount,
            is_bbox_fallback=True,
        )

    def _check_courtyard_overlap_polygon(
        self, c1: ComponentInfo, c2: ComponentInfo
    ) -> Conflict | None:
        """Positive-area courtyard-polygon overlap test (issue #4182).

        Mirrors ``CourtyardOverlapRule``: two courtyards conflict only when
        their real polygons intersect with strictly positive area (exactly
        touching, i.e. zero-area, does not conflict).  The reported overlap
        amount is the square root of the intersection area, keeping the same
        millimetre-scaled ``overlap_amount`` units the bbox path emits.
        """
        poly_a = c1.courtyard_polygon
        poly_b = c2.courtyard_polygon
        if poly_a is None or poly_b is None:
            return None

        if not poly_a.intersects(poly_b):
            return None
        inter = poly_a.intersection(poly_b)
        if inter.is_empty or inter.area <= 0:
            # Exactly-touching (zero-area) courtyards do not conflict.
            return None

        overlap_amount = math.sqrt(inter.area)
        centroid = inter.centroid

        return Conflict(
            type=ConflictType.COURTYARD_OVERLAP,
            severity=ConflictSeverity.WARNING,
            component1=c1.reference,
            component2=c2.reference,
            message=f"courtyards overlap by {inter.area:.3f}mm^2",
            location=Point(centroid.x, centroid.y),
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
                        ConflictSeverity.ERROR if clearance <= 0 else ConflictSeverity.WARNING
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
                        ConflictSeverity.ERROR if edge_dist <= 0 else ConflictSeverity.WARNING
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

    def _check_off_board(self) -> Iterator[Conflict]:
        """Flag components whose courtyard falls outside the board outline.

        A footprint whose courtyard is *fully* outside the outline (the common
        "everything shifted N mm off the board" incident, issue #4156), or
        whose **pads** extend past the edge, cannot be manufactured or routed
        as placed — those are unconditional ``ERROR`` severity, independent of
        ``min_edge_clearance``.

        A footprint whose courtyard *artwork* overhangs the edge while every
        pad sits on the board is different (issue #4290): edge-overhang parts
        — chip-antenna / RF modules (e.g. ESP32-C3-WROOM-02, whose antenna
        keepout courtyard is *supposed* to protrude past Edge.Cuts per the
        datasheet), card-edge USB, and overhanging connectors — do this by
        design.  Those are reported as ``WARNING`` so ``kct placement check``
        still surfaces them without invalidating the placement (and without
        forcing ``kct route --allow-offboard``, which would also disable the
        real off-board gate).

        Uses an axis-aligned bounding-box test against the outline bbox — the
        same cheap approach as ``place_unplaced._get_board_bounds``.  A true
        polygon test for non-rectangular outlines (cutouts, rounded corners)
        is a future refinement (see issue #4182); the bbox test covers the
        reported incident and never false-negatives on a shifted-off-board
        row.

        All comparisons happen in a single coordinate frame: both the outline
        bbox (:meth:`_extract_board_edge`) and every pad/courtyard coordinate
        come from the same board-relative frame that ``schema.PCB`` normalizes
        on load.  The message additionally reports the outline in
        sheet-absolute coordinates when the board origin is non-zero, so the
        printed bounds match what KiCad displays for offset boards instead of
        the misleading-looking ``0.0,0.0 to W,H``.
        """
        if not self._board_edge:
            return

        edge = self._board_edge
        # Float-noise tolerance for the pads-on-board containment test (the
        # board-relative frame is produced by subtraction, so exact equality
        # at the edge is not guaranteed).
        eps = 1e-6

        for comp in self._components:
            if not comp.courtyard:
                continue

            cy = comp.courtyard

            fully_outside = (
                cy.max_x <= edge.min_x
                or cy.min_x >= edge.max_x
                or cy.max_y <= edge.min_y
                or cy.min_y >= edge.max_y
            )
            partially_outside = (
                cy.min_x < edge.min_x
                or cy.max_x > edge.max_x
                or cy.min_y < edge.min_y
                or cy.max_y > edge.max_y
            )

            if not fully_outside and not partially_outside:
                continue

            # Which side(s) overhang, and by how much — makes the real cause
            # (e.g. a 2.35 mm antenna-courtyard overhang) instantly visible.
            overhangs: list[str] = []
            if cy.min_x < edge.min_x:
                overhangs.append(f"left {edge.min_x - cy.min_x:.2f}mm")
            if cy.max_x > edge.max_x:
                overhangs.append(f"right {cy.max_x - edge.max_x:.2f}mm")
            if cy.min_y < edge.min_y:
                overhangs.append(f"top {edge.min_y - cy.min_y:.2f}mm")
            if cy.max_y > edge.max_y:
                overhangs.append(f"bottom {cy.max_y - edge.max_y:.2f}mm")
            overhang_txt = ", ".join(overhangs)

            board_txt = self._board_bounds_text(edge)

            pads_on_board = comp.pads_bbox is not None and (
                comp.pads_bbox.min_x >= edge.min_x - eps
                and comp.pads_bbox.max_x <= edge.max_x + eps
                and comp.pads_bbox.min_y >= edge.min_y - eps
                and comp.pads_bbox.max_y <= edge.max_y + eps
            )

            if not fully_outside and pads_on_board:
                # Courtyard-artwork-only edge overhang with every pad on the
                # board: probable intentional overhang (issue #4290).
                yield Conflict(
                    type=ConflictType.OFF_BOARD,
                    severity=ConflictSeverity.WARNING,
                    component1=comp.reference,
                    component2="board_outline",
                    message=(
                        f"courtyard overhangs Edge.Cuts outline ({overhang_txt}; "
                        f"{board_txt}) but all pads are on-board — probable "
                        "intentional edge overhang (antenna/connector); verify "
                        "mechanical fit"
                    ),
                    location=cy.center,
                )
                continue

            descriptor = "fully outside" if fully_outside else "partially outside"
            yield Conflict(
                type=ConflictType.OFF_BOARD,
                severity=ConflictSeverity.ERROR,
                component1=comp.reference,
                component2="board_outline",
                message=(
                    f"courtyard {descriptor} Edge.Cuts outline "
                    f"({overhang_txt}; {board_txt}) — placement invalid"
                ),
                location=cy.center,
            )

    def _board_bounds_text(self, edge: Rectangle) -> str:
        """Human-readable outline bounds for off-board messages.

        Reports the board-relative bbox, plus the sheet-absolute bbox when the
        detected board origin is non-zero.  On an offset board (Edge.Cuts min
        corner not at the sheet origin) a bare ``board 0.0,0.0 to 160.0,100.0``
        reads as if the checker normalized the outline while leaving pads
        absolute (issue #4290); echoing the sheet coordinates KiCad displays
        removes that ambiguity.
        """
        ox, oy = self._board_origin_offset
        text = f"board {edge.min_x:.1f},{edge.min_y:.1f} to {edge.max_x:.1f},{edge.max_y:.1f}"
        if ox != 0.0 or oy != 0.0:
            text += (
                f" board-relative = sheet {edge.min_x + ox:.1f},{edge.min_y + oy:.1f}"
                f" to {edge.max_x + ox:.1f},{edge.max_y + oy:.1f}"
            )
        return text

    def _check_edge_clearance(self, min_clearance: float) -> Iterator[Conflict]:
        """Check clearance from components to board edge.

        This reports components that are *inside* the outline but closer to an
        edge than ``min_clearance`` (a tightness warning/error).  Components
        that fall outside the outline entirely are handled separately by
        :meth:`_check_off_board`, which reports a distinct ``OFF_BOARD``
        error; skip them here so a single off-board footprint is not
        double-reported as four edge violations.
        """
        if not self._board_edge:
            return

        edge = self._board_edge

        for comp in self._components:
            if not comp.courtyard:
                continue

            cy = comp.courtyard

            # Skip components handled by _check_off_board (outside the outline).
            outside = (
                cy.min_x < edge.min_x
                or cy.max_x > edge.max_x
                or cy.min_y < edge.min_y
                or cy.max_y > edge.max_y
            )
            if outside:
                continue

            # Check each edge
            violations: list[tuple[str, float, Point]] = []

            # Left edge
            if cy.min_x < edge.min_x + min_clearance:
                dist = cy.min_x - edge.min_x
                violations.append(
                    (
                        "left",
                        dist,
                        Point(edge.min_x, (cy.min_y + cy.max_y) / 2),
                    )
                )

            # Right edge
            if cy.max_x > edge.max_x - min_clearance:
                dist = edge.max_x - cy.max_x
                violations.append(
                    (
                        "right",
                        dist,
                        Point(edge.max_x, (cy.min_y + cy.max_y) / 2),
                    )
                )

            # Top edge
            if cy.min_y < edge.min_y + min_clearance:
                dist = cy.min_y - edge.min_y
                violations.append(
                    (
                        "top",
                        dist,
                        Point((cy.min_x + cy.max_x) / 2, edge.min_y),
                    )
                )

            # Bottom edge
            if cy.max_y > edge.max_y - min_clearance:
                dist = edge.max_y - cy.max_y
                violations.append(
                    (
                        "bottom",
                        dist,
                        Point((cy.min_x + cy.max_x) / 2, edge.max_y),
                    )
                )

            for edge_name, dist, loc in violations:
                severity = ConflictSeverity.ERROR if dist < 0 else ConflictSeverity.WARNING

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

    def _check_edge_connectors(self, max_inset: float) -> Iterator[Conflict]:
        """Flag off-board connectors marooned in the board interior.

        An "edge connector" — USB, barrel jack, RJ45, card-edge finger, or a
        cable header — must sit at the board **perimeter** so its mating face
        is accessible off the board.  A connector whose courtyard is *fully
        inside* the outline yet stands off from the nearest edge by more than
        ``max_inset`` mm is almost certainly mis-placed: no cable can physically
        reach it.  This is the motivating case for issue #4450 — board-03's
        USB-C ``J1`` sits 8 mm inside the north edge with its mouth facing the
        board interior, so a USB cable cannot plug in.

        Connector classification reuses the shared detector in
        :mod:`kicad_tools.optim.edge_placement` (``_is_connector``, keyed on
        reference designator and footprint-name keywords) rather than a private
        keyword list, so the placement checker and the placement optimizer never
        drift apart on what counts as a connector.

        Only components whose courtyard is fully inside the outline are
        considered.  A connector whose courtyard overhangs or falls outside the
        edge is either an intentional edge overhang or a shifted-off-board part
        already reported by :meth:`_check_off_board`; skipping those avoids
        double-reporting and means a correctly *edge-placed* connector (its
        courtyard touching the edge) never trips this check.

        This is a heuristic surfaced at ``WARNING`` severity: some connectors
        (board-to-board mezzanine, internal jumpers) are legitimately interior,
        so the finding invites review rather than invalidating the placement.
        """
        if not self._board_edge:
            return

        from kicad_tools.optim.edge_placement import _is_connector

        edge = self._board_edge
        # Float-noise tolerance for the fully-inside containment test (the
        # board-relative frame is produced by subtraction).
        eps = 1e-6

        for comp in self._components:
            if not comp.courtyard:
                continue
            if not _is_connector(comp.reference, comp.footprint):
                continue

            cy = comp.courtyard

            # Skip parts not fully inside the outline — those are handled by
            # _check_off_board (overhang / shifted-off-board).  A connector
            # placed flush at the edge has its courtyard touching (or slightly
            # past) the outline, so it is excluded here by design.
            outside = (
                cy.min_x < edge.min_x - eps
                or cy.max_x > edge.max_x + eps
                or cy.min_y < edge.min_y - eps
                or cy.max_y > edge.max_y + eps
            )
            if outside:
                continue

            # Distance from the courtyard to each of the four board edges; the
            # smallest is how far the connector stands off the perimeter.
            gaps = {
                "left": cy.min_x - edge.min_x,
                "right": edge.max_x - cy.max_x,
                "top": cy.min_y - edge.min_y,
                "bottom": edge.max_y - cy.max_y,
            }
            nearest_edge = min(gaps, key=lambda k: gaps[k])
            inset = gaps[nearest_edge]

            if inset <= max_inset:
                # Adjacent to an edge — correctly perimeter-placed.
                continue

            yield Conflict(
                type=ConflictType.EDGE_CONNECTOR_PLACEMENT,
                severity=ConflictSeverity.WARNING,
                component1=comp.reference,
                component2=f"{nearest_edge}_edge",
                message=(
                    f"off-board connector {comp.reference} is {inset:.1f}mm inside "
                    f"the board interior (nearest edge: {nearest_edge}); off-board "
                    "connectors must sit at the perimeter with the mating face "
                    "accessible off-board"
                ),
                location=cy.center,
                actual_clearance=inset,
                required_clearance=max_inset,
            )

    def _extract_board_edge(self, pcb) -> Rectangle | None:
        """Extract board outline bounding box from the Edge.Cuts layer.

        Real board outlines — including every board produced by
        ``kct create-pcb`` (``PCB.create``) and every board KiCad itself
        writes — are drawn as ``gr_line`` / ``gr_arc`` / ``gr_rect`` /
        ``gr_poly`` graphics, **not** copper ``(segment ...)`` elements.  An
        earlier implementation read the outline via
        ``pcb.segments_on_layer("Edge.Cuts")``, which only ever returns copper
        segments, so it returned ``None`` on every normally-produced board.
        That silently disabled the edge-clearance / off-board checks (issue
        #4156).

        This now delegates to :meth:`PCB.get_board_outline`, which correctly
        assembles the outline from graphics and returns board-relative
        coordinates — the same reusable, already-correct path used by
        ``placement/place_unplaced.py::_get_board_bounds``.

        Returns bounding box of the board edge, or ``None`` when the board has
        no Edge.Cuts outline at all.
        """
        outline = pcb.get_board_outline()
        if not outline:
            return None

        # Remember where the board-relative frame sits on the sheet so
        # off-board messages can echo sheet-absolute coordinates (#4290).
        try:
            origin = pcb.board_origin
            self._board_origin_offset = (float(origin[0]), float(origin[1]))
        except Exception:
            self._board_origin_offset = (0.0, 0.0)

        # get_board_outline() already returns board-relative coordinates.
        all_x = [p[0] for p in outline]
        all_y = [p[1] for p in outline]

        if not all_x:
            return None

        return Rectangle(min(all_x), min(all_y), max(all_x), max(all_y))

    def get_components(self) -> list[ComponentInfo]:
        """Get list of analyzed components."""
        return self._components

    def get_board_edge(self) -> Rectangle | None:
        """Get board edge bounding box."""
        return self._board_edge
