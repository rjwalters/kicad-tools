"""Drill clearance repair tool - fix via-to-via and via-to-pad clearance violations.

This module provides repair strategies for drill clearance violations:

1. Same-net coincident vias: de-duplicate (remove redundant vias)
2. Different-net vias too close: slide one via along its connecting trace

Usage:
    from kicad_tools.drc.repair_drill_clearance import DrillClearanceRepairer

    repairer = DrillClearanceRepairer("board.kicad_pcb")
    results = repairer.repair(violations, max_displacement=0.5)
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..sexp import SExp, parse_file
from .violation import DRCViolation, ViolationType


@dataclass
class DrillRepairAction:
    """Record of a single drill clearance repair action."""

    action: str  # "deduplicate" or "slide"
    via_x: float
    via_y: float
    net_name: str
    detail: str  # human-readable description
    displacement_mm: float = 0.0  # 0 for dedup

    def __str__(self) -> str:
        return f"{self.action}: via [{self.net_name}] at ({self.via_x:.4f}, {self.via_y:.4f}) - {self.detail}"


@dataclass
class DrillRepairResult:
    """Summary of a drill clearance repair operation."""

    total_violations: int = 0
    repaired: int = 0
    deduplicated: int = 0
    slid: int = 0
    skipped_no_location: int = 0
    skipped_no_delta: int = 0
    skipped_exceeds_max: int = 0
    skipped_infeasible: int = 0
    actions: list[DrillRepairAction] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Fraction of violations that were repaired."""
        if self.total_violations == 0:
            return 1.0
        return self.repaired / self.total_violations

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            f"Drill Clearance Repair: {self.repaired}/{self.total_violations} violations fixed",
        ]
        if self.deduplicated > 0:
            lines.append(f"  De-duplicated same-net vias: {self.deduplicated}")
        if self.slid > 0:
            lines.append(f"  Slid vias apart: {self.slid}")
        if self.skipped_exceeds_max > 0:
            lines.append(f"  Skipped (exceeds max displacement): {self.skipped_exceeds_max}")
        if self.skipped_infeasible > 0:
            lines.append(f"  Skipped (infeasible): {self.skipped_infeasible}")
        if self.skipped_no_location > 0:
            lines.append(f"  Skipped (no location): {self.skipped_no_location}")
        if self.skipped_no_delta > 0:
            lines.append(f"  Skipped (no delta info): {self.skipped_no_delta}")
        return "\n".join(lines)


class DrillClearanceRepairer:
    """Repairs drill clearance violations.

    Two repair strategies:
    1. Same-net coincident vias: remove duplicate (de-duplication)
    2. Different-net vias too close: slide one via along its connecting trace
    """

    COINCIDENT_THRESHOLD = 0.01  # mm - vias closer than this are "coincident"

    def __init__(self, pcb_path: str | Path):
        """Load a PCB file for repair.

        Args:
            pcb_path: Path to .kicad_pcb file
        """
        self.path = Path(pcb_path)
        self.doc = parse_file(self.path)
        self.modified = False

        # Build net index
        self.nets: dict[int, str] = {}
        self.net_names: dict[str, int] = {}
        self._parse_nets()

    def _parse_nets(self) -> None:
        """Parse net definitions from the PCB."""
        for net_node in self.doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_num = int(atoms[0])
                net_name = str(atoms[1])
                self.nets[net_num] = net_name
                self.net_names[net_name] = net_num

    def repair(
        self,
        violations: list[DRCViolation],
        max_displacement: float = 0.5,
        margin: float = 0.01,
        dry_run: bool = False,
    ) -> DrillRepairResult:
        """Repair drill clearance violations.

        Args:
            violations: List of drill clearance violations
            max_displacement: Maximum allowed slide distance in mm
            margin: Extra clearance margin beyond minimum in mm
            dry_run: If True, compute repairs but don't modify PCB

        Returns:
            DrillRepairResult with details of all repairs
        """
        result = DrillRepairResult()

        # Filter to only drill clearance violations
        drill_violations = [
            v
            for v in violations
            if v.type in (ViolationType.DRILL_CLEARANCE, ViolationType.HOLE_NEAR_HOLE)
        ]
        result.total_violations = len(drill_violations)

        for violation in drill_violations:
            self._repair_single(violation, result, max_displacement, margin, dry_run)

        return result

    def _repair_single(
        self,
        violation: DRCViolation,
        result: DrillRepairResult,
        max_displacement: float,
        margin: float,
        dry_run: bool,
    ) -> None:
        """Attempt to repair a single drill clearance violation."""
        if not violation.locations or len(violation.locations) < 1:
            result.skipped_no_location += 1
            return

        delta = violation.delta_mm
        if delta is None:
            result.skipped_no_delta += 1
            return

        loc = violation.locations[0]

        # Find the two vias near the violation location
        vias = self._find_vias_near(loc.x_mm, loc.y_mm, radius=2.0)

        if len(vias) < 2:
            # Try with the second location if available
            if len(violation.locations) >= 2:
                loc2 = violation.locations[1]
                vias2 = self._find_vias_near(loc2.x_mm, loc2.y_mm, radius=2.0)
                # Merge, dedup by node identity
                seen_nodes = {id(v[0]) for v in vias}
                for v in vias2:
                    if id(v[0]) not in seen_nodes:
                        vias.append(v)
                        seen_nodes.add(id(v[0]))

        if len(vias) < 2:
            result.skipped_infeasible += 1
            return

        # Sort by distance to violation point for consistent behavior
        vias.sort(key=lambda v: math.sqrt((v[2] - loc.x_mm) ** 2 + (v[3] - loc.y_mm) ** 2))

        via1_node, via1_net, via1_x, via1_y, via1_drill = vias[0]
        via2_node, via2_net, via2_x, via2_y, via2_drill = vias[1]

        dist = math.sqrt((via2_x - via1_x) ** 2 + (via2_y - via1_y) ** 2)

        # Check if same-net coincident vias (de-duplication candidate)
        if via1_net == via2_net and dist < self.COINCIDENT_THRESHOLD:
            self._deduplicate(via2_node, via2_x, via2_y, via2_net, result, dry_run)
            return

        # Different-net or non-coincident: slide one via apart
        required_displacement = delta + margin

        if required_displacement > max_displacement:
            result.skipped_exceeds_max += 1
            return

        self._slide_via(
            via2_node,
            via2_x,
            via2_y,
            via2_net,
            via2_drill,
            via1_x,
            via1_y,
            via1_drill,
            required_displacement,
            result,
            dry_run,
        )

    def _find_vias_near(
        self,
        x: float,
        y: float,
        radius: float,
    ) -> list[tuple[SExp, str, float, float, float]]:
        """Find vias near a point.

        Returns list of (node, net_name, x, y, drill).
        """
        results = []

        for via_node in self.doc.find_all("via"):
            at_node = via_node.find("at")
            if not at_node:
                continue

            at_atoms = at_node.get_atoms()
            vx = float(at_atoms[0]) if at_atoms else 0
            vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0

            dist = math.sqrt((vx - x) ** 2 + (vy - y) ** 2)
            if dist > radius:
                continue

            net_node = via_node.find("net")
            net_num = int(net_node.get_first_atom()) if net_node else 0
            net_name = self.nets.get(net_num, "")

            drill_node = via_node.find("drill")
            drill = float(drill_node.get_first_atom()) if drill_node else 0.3

            results.append((via_node, net_name, vx, vy, drill))

        return results

    def _deduplicate(
        self,
        via_node: SExp,
        via_x: float,
        via_y: float,
        net_name: str,
        result: DrillRepairResult,
        dry_run: bool,
    ) -> None:
        """Remove a duplicate same-net via."""
        action = DrillRepairAction(
            action="deduplicate",
            via_x=via_x,
            via_y=via_y,
            net_name=net_name,
            detail="removed duplicate same-net via",
            displacement_mm=0.0,
        )
        result.actions.append(action)

        if not dry_run:
            # Remove the via from the document
            for i, child in enumerate(self.doc.children):
                if child is via_node:
                    del self.doc.children[i]
                    self.modified = True
                    break

        result.repaired += 1
        result.deduplicated += 1

    def _slide_via(
        self,
        via_node: SExp,
        via_x: float,
        via_y: float,
        via_net: str,
        via_drill: float,
        other_x: float,
        other_y: float,
        other_drill: float,
        required_displacement: float,
        result: DrillRepairResult,
        dry_run: bool,
    ) -> None:
        """Slide a via away from another object to achieve clearance.

        Finds a connected trace segment and slides the via along it.
        Falls back to pushing via directly away if no connected trace exists.
        """
        # Find connected segment to determine slide direction
        connected_seg = self._find_connected_segment(via_x, via_y, via_net)

        if connected_seg is not None:
            # Slide along the connected segment direction
            seg_node, sx, sy, ex, ey = connected_seg

            # Determine which endpoint is the via
            d_start = math.sqrt((sx - via_x) ** 2 + (sy - via_y) ** 2)
            d_end = math.sqrt((ex - via_x) ** 2 + (ey - via_y) ** 2)

            if d_start < d_end:
                # Via is at the start, slide toward the end
                dir_x, dir_y = ex - sx, ey - sy
            else:
                # Via is at the end, slide toward the start
                dir_x, dir_y = sx - ex, sy - ey

            dir_len = math.sqrt(dir_x**2 + dir_y**2)
            if dir_len < 1e-10:
                # Zero-length segment, fall back to direct push
                dx, dy, dist = self._compute_push_away(
                    via_x, via_y, other_x, other_y, required_displacement
                )
            else:
                # Determine which direction along the segment moves us away
                # from the other object
                norm_x = dir_x / dir_len
                norm_y = dir_y / dir_len

                # Check if moving in this direction increases distance
                test_x = via_x + norm_x * required_displacement
                test_y = via_y + norm_y * required_displacement
                new_dist = math.sqrt((test_x - other_x) ** 2 + (test_y - other_y) ** 2)
                old_dist = math.sqrt((via_x - other_x) ** 2 + (via_y - other_y) ** 2)

                if new_dist <= old_dist:
                    # Reverse direction
                    norm_x, norm_y = -norm_x, -norm_y

                dx = norm_x * required_displacement
                dy = norm_y * required_displacement
                dist = required_displacement

                # Also update the segment endpoint that connects to the via
                if not dry_run:
                    self._update_segment_endpoint(seg_node, via_x, via_y, via_x + dx, via_y + dy)
        else:
            # No connected segment found; push via directly away
            dx, dy, dist = self._compute_push_away(
                via_x, via_y, other_x, other_y, required_displacement
            )

        action = DrillRepairAction(
            action="slide",
            via_x=via_x,
            via_y=via_y,
            net_name=via_net,
            detail=f"slid {dist:.4f}mm to increase clearance",
            displacement_mm=dist,
        )
        result.actions.append(action)

        if not dry_run:
            at_node = via_node.find("at")
            if at_node:
                at_node.set_value(0, round(via_x + dx, 4))
                at_node.set_value(1, round(via_y + dy, 4))
                self.modified = True

        result.repaired += 1
        result.slid += 1

    def _compute_push_away(
        self,
        obj_x: float,
        obj_y: float,
        other_x: float,
        other_y: float,
        displacement: float,
    ) -> tuple[float, float, float]:
        """Compute displacement to push obj away from other.

        Returns (dx, dy, distance).
        """
        dir_x = obj_x - other_x
        dir_y = obj_y - other_y
        d = math.sqrt(dir_x**2 + dir_y**2)

        if d < 1e-10:
            # Coincident, push in +x direction
            return (displacement, 0.0, displacement)

        scale = displacement / d
        return (dir_x * scale, dir_y * scale, displacement)

    def _find_connected_segment(
        self,
        via_x: float,
        via_y: float,
        net_name: str,
    ) -> tuple[SExp, float, float, float, float] | None:
        """Find a trace segment connected to a via.

        Returns (seg_node, start_x, start_y, end_x, end_y) or None.
        """
        net_num = self.net_names.get(net_name, -1)
        tolerance = 0.01  # mm

        for seg_node in self.doc.find_all("segment"):
            net_node = seg_node.find("net")
            if not net_node:
                continue
            seg_net = int(net_node.get_first_atom())
            if seg_net != net_num:
                continue

            start_node = seg_node.find("start")
            end_node = seg_node.find("end")
            if not (start_node and end_node):
                continue

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()

            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            # Check if via is at either endpoint
            d_start = math.sqrt((sx - via_x) ** 2 + (sy - via_y) ** 2)
            d_end = math.sqrt((ex - via_x) ** 2 + (ey - via_y) ** 2)

            if d_start < tolerance or d_end < tolerance:
                return (seg_node, sx, sy, ex, ey)

        return None

    def _update_segment_endpoint(
        self,
        seg_node: SExp,
        old_x: float,
        old_y: float,
        new_x: float,
        new_y: float,
    ) -> None:
        """Update the segment endpoint that matches old_x, old_y."""
        tolerance = 0.01

        start_node = seg_node.find("start")
        end_node = seg_node.find("end")

        if start_node:
            start_atoms = start_node.get_atoms()
            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            if math.sqrt((sx - old_x) ** 2 + (sy - old_y) ** 2) < tolerance:
                start_node.set_value(0, round(new_x, 4))
                start_node.set_value(1, round(new_y, 4))
                return

        if end_node:
            end_atoms = end_node.get_atoms()
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0
            if math.sqrt((ex - old_x) ** 2 + (ey - old_y) ** 2) < tolerance:
                end_node.set_value(0, round(new_x, 4))
                end_node.set_value(1, round(new_y, 4))

    def save(self, output_path: str | Path | None = None) -> None:
        """Save the modified PCB.

        Args:
            output_path: Path to save to. If None, overwrites the input file.
        """
        from ..core.sexp_file import save_pcb

        path = Path(output_path) if output_path else self.path
        save_pcb(self.doc, path)
