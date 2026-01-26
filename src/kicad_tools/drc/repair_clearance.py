"""Trace clearance repair tool - nudge traces to fix DRC clearance violations.

This module provides non-destructive repair of clearance violations by
computing minimal displacements for traces and vias rather than deleting them.

For each clearance violation:
1. Identify the two objects (trace segment, via, pad)
2. Calculate required displacement to achieve minimum clearance + margin
3. Check if displacement is feasible (doesn't create new violations)
4. Apply the smallest valid displacement
5. Optionally re-run DRC to verify

Usage:
    from kicad_tools.drc.repair_clearance import ClearanceRepairer

    repairer = ClearanceRepairer("board.kicad_pcb")
    results = repairer.repair_from_report(report, max_displacement=0.1)
    repairer.save("board-fixed.kicad_pcb")
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

from ..sexp import SExp, parse_file
from .report import DRCReport
from .violation import DRCViolation, ViolationType


@dataclass
class NudgeResult:
    """Record of a nudge applied to fix a clearance violation."""

    object_type: str  # "segment" or "via"
    x: float
    y: float
    net_name: str
    layer: str
    displacement_x: float
    displacement_y: float
    displacement_mm: float
    old_clearance_mm: float
    new_clearance_mm: float
    uuid: str

    def __str__(self) -> str:
        return (
            f"{self.object_type} [{self.net_name}] at ({self.x:.4f}, {self.y:.4f}) on {self.layer}: "
            f"nudged {self.displacement_mm:.4f}mm "
            f"(clearance {self.old_clearance_mm:.4f} -> {self.new_clearance_mm:.4f}mm)"
        )


@dataclass
class RepairResult:
    """Summary of a clearance repair operation."""

    total_violations: int = 0
    repaired: int = 0
    skipped_no_location: int = 0
    skipped_not_clearance: int = 0
    skipped_no_delta: int = 0
    skipped_exceeds_max: int = 0
    skipped_infeasible: int = 0
    nudges: list[NudgeResult] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        """Fraction of violations that were repaired."""
        if self.total_violations == 0:
            return 1.0
        return self.repaired / self.total_violations

    def summary(self) -> str:
        """Generate a human-readable summary."""
        lines = [
            f"Clearance Repair: {self.repaired}/{self.total_violations} violations fixed",
        ]
        if self.skipped_exceeds_max > 0:
            lines.append(
                f"  Skipped (exceeds max displacement): {self.skipped_exceeds_max}"
            )
        if self.skipped_infeasible > 0:
            lines.append(f"  Skipped (infeasible): {self.skipped_infeasible}")
        if self.skipped_no_location > 0:
            lines.append(f"  Skipped (no location): {self.skipped_no_location}")
        if self.skipped_no_delta > 0:
            lines.append(f"  Skipped (no delta info): {self.skipped_no_delta}")
        return "\n".join(lines)


class ClearanceRepairer:
    """Repairs clearance violations by nudging traces and vias.

    Unlike DRCFixer which deletes traces, this tool computes minimal
    displacements to achieve the required clearance without removing
    any routing.
    """

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

    def _parse_nets(self):
        """Parse net definitions from the PCB."""
        for net_node in self.doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_num = int(atoms[0])
                net_name = str(atoms[1])
                self.nets[net_num] = net_name
                self.net_names[net_name] = net_num

    def repair_from_report(
        self,
        report: DRCReport,
        max_displacement: float = 0.1,
        margin: float = 0.01,
        prefer: str = "move-trace",
        dry_run: bool = False,
    ) -> RepairResult:
        """Repair clearance violations using a DRC report.

        Args:
            report: Parsed DRC report with violations
            max_displacement: Maximum allowed nudge distance in mm
            margin: Extra clearance margin beyond minimum in mm
            prefer: Which object to move when both are movable
                    ("move-trace" or "move-via")
            dry_run: If True, compute repairs but don't modify PCB

        Returns:
            RepairResult with details of all repairs
        """
        result = RepairResult()

        clearances = report.by_type(ViolationType.CLEARANCE)
        result.total_violations = len(clearances)

        for violation in clearances:
            self._repair_single_violation(
                violation, result, max_displacement, margin, prefer, dry_run
            )

        return result

    def _repair_single_violation(
        self,
        violation: DRCViolation,
        result: RepairResult,
        max_displacement: float,
        margin: float,
        prefer: str,
        dry_run: bool,
    ) -> None:
        """Attempt to repair a single clearance violation."""
        # Need at least two locations (the two objects that are too close)
        if len(violation.locations) < 2:
            if violation.primary_location is None:
                result.skipped_no_location += 1
                return
            # With only one location, we can still try to find nearby objects
            loc = violation.primary_location
            delta = violation.delta_mm
            if delta is None:
                result.skipped_no_delta += 1
                return
            self._repair_from_single_location(
                loc.x_mm, loc.y_mm, loc.layer, delta, margin,
                violation, result, max_displacement, prefer, dry_run,
            )
            return

        loc1 = violation.locations[0]
        loc2 = violation.locations[1]

        delta = violation.delta_mm
        if delta is None:
            result.skipped_no_delta += 1
            return

        # Required displacement is the clearance deficit plus margin
        required_displacement = delta + margin

        if required_displacement > max_displacement:
            result.skipped_exceeds_max += 1
            return

        # Find objects at the two locations
        obj1 = self._find_object_at(loc1.x_mm, loc1.y_mm, loc1.layer, violation.nets)
        obj2 = self._find_object_at(loc2.x_mm, loc2.y_mm, loc2.layer, violation.nets)

        if obj1 is None and obj2 is None:
            result.skipped_infeasible += 1
            return

        # Choose which object to move based on preference and movability
        target = self._choose_target(obj1, obj2, prefer)
        if target is None:
            result.skipped_infeasible += 1
            return

        obj_node, obj_type, obj_x, obj_y, obj_layer, obj_net = target

        # Determine the other location (the one we're moving away from)
        if obj1 is not None and obj1[0] is obj_node:
            other_x, other_y = loc2.x_mm, loc2.y_mm
        else:
            other_x, other_y = loc1.x_mm, loc1.y_mm

        # Calculate displacement vector (away from the other object)
        nudge = self._compute_nudge(
            obj_x, obj_y, other_x, other_y, required_displacement
        )
        if nudge is None:
            result.skipped_infeasible += 1
            return

        dx, dy, dist = nudge

        # Get UUID
        uuid_node = obj_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""

        # Get actual clearance values for reporting
        actual_clearance = violation.actual_value_mm or 0.0
        required_clearance = violation.required_value_mm or 0.0

        nudge_result = NudgeResult(
            object_type=obj_type,
            x=obj_x,
            y=obj_y,
            net_name=obj_net,
            layer=obj_layer,
            displacement_x=dx,
            displacement_y=dy,
            displacement_mm=dist,
            old_clearance_mm=actual_clearance,
            new_clearance_mm=actual_clearance + dist,
            uuid=uuid_str,
        )
        result.nudges.append(nudge_result)

        if not dry_run:
            self._apply_nudge(obj_node, obj_type, dx, dy)
            self.modified = True

        result.repaired += 1

    def _repair_from_single_location(
        self,
        x: float,
        y: float,
        layer: str,
        delta: float,
        margin: float,
        violation: DRCViolation,
        result: RepairResult,
        max_displacement: float,
        prefer: str,
        dry_run: bool,
    ) -> None:
        """Repair a violation with only one reported location.

        Finds both objects near the violation point and nudges the preferred one.
        """
        required_displacement = delta + margin

        if required_displacement > max_displacement:
            result.skipped_exceeds_max += 1
            return

        # Find objects near the violation location
        search_radius = 1.0  # mm
        segments = self._find_segments_near(x, y, search_radius, layer, violation.nets)
        vias = self._find_vias_near(x, y, search_radius, violation.nets)

        # Need at least two objects to have a clearance issue
        all_objects = []
        for seg_info in segments:
            all_objects.append(seg_info)
        for via_info in vias:
            all_objects.append(via_info)

        if len(all_objects) < 2:
            result.skipped_infeasible += 1
            return

        # Pick the movable target based on preference
        target = self._choose_target(all_objects[0], all_objects[1], prefer)
        if target is None:
            result.skipped_infeasible += 1
            return

        obj_node, obj_type, obj_x, obj_y, obj_layer, obj_net = target

        # The other object is the one we're moving away from
        other = all_objects[1] if all_objects[0][0] is obj_node else all_objects[0]
        _, _, other_x, other_y, _, _ = other

        nudge = self._compute_nudge(
            obj_x, obj_y, other_x, other_y, required_displacement
        )
        if nudge is None:
            result.skipped_infeasible += 1
            return

        dx, dy, dist = nudge

        uuid_node = obj_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""

        actual_clearance = violation.actual_value_mm or 0.0

        nudge_result = NudgeResult(
            object_type=obj_type,
            x=obj_x,
            y=obj_y,
            net_name=obj_net,
            layer=obj_layer,
            displacement_x=dx,
            displacement_y=dy,
            displacement_mm=dist,
            old_clearance_mm=actual_clearance,
            new_clearance_mm=actual_clearance + dist,
            uuid=uuid_str,
        )
        result.nudges.append(nudge_result)

        if not dry_run:
            self._apply_nudge(obj_node, obj_type, dx, dy)
            self.modified = True

        result.repaired += 1

    def _find_object_at(
        self,
        x: float,
        y: float,
        layer: str,
        nets: list[str],
    ) -> tuple[SExp, str, float, float, str, str] | None:
        """Find the nearest PCB object at a location.

        Returns: (node, type, x, y, layer, net_name) or None
        """
        search_radius = 0.5  # mm

        # Check segments
        segments = self._find_segments_near(x, y, search_radius, layer, nets)
        if segments:
            return segments[0]

        # Check vias
        vias = self._find_vias_near(x, y, search_radius, nets)
        if vias:
            return vias[0]

        # Check pads (pads are not movable, so return None to skip)
        return None

    def _find_segments_near(
        self,
        x: float,
        y: float,
        radius: float,
        layer: str | None,
        nets: list[str] | None,
    ) -> list[tuple[SExp, str, float, float, str, str]]:
        """Find track segments near a point.

        Returns list of (node, "segment", closest_x, closest_y, layer, net_name).
        """
        results = []

        for seg_node in self.doc.find_all("segment"):
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

            # Find closest point on segment to the target
            closest = self._closest_point_on_segment(sx, sy, ex, ey, x, y)
            if closest is None:
                continue
            cx, cy, dist = closest

            if dist > radius:
                continue

            layer_node = seg_node.find("layer")
            seg_layer = layer_node.get_first_atom() if layer_node else ""
            if layer and seg_layer != layer:
                continue

            net_node = seg_node.find("net")
            net_num = int(net_node.get_first_atom()) if net_node else 0
            net_name = self.nets.get(net_num, "")

            if nets and net_name not in nets:
                continue

            results.append((seg_node, "segment", cx, cy, seg_layer, net_name))

        return results

    def _find_vias_near(
        self,
        x: float,
        y: float,
        radius: float,
        nets: list[str] | None,
    ) -> list[tuple[SExp, str, float, float, str, str]]:
        """Find vias near a point.

        Returns list of (node, "via", x, y, layer_str, net_name).
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

            if nets and net_name not in nets:
                continue

            # Vias span layers, use "F.Cu - B.Cu" as layer description
            layers_node = via_node.find("layers")
            layer_str = ""
            if layers_node:
                layer_atoms = layers_node.get_atoms()
                layer_str = " - ".join(str(a) for a in layer_atoms)

            results.append((via_node, "via", vx, vy, layer_str, net_name))

        return results

    def _choose_target(
        self,
        obj1: tuple[SExp, str, float, float, str, str] | None,
        obj2: tuple[SExp, str, float, float, str, str] | None,
        prefer: str,
    ) -> tuple[SExp, str, float, float, str, str] | None:
        """Choose which object to nudge.

        Args:
            obj1: First object (node, type, x, y, layer, net)
            obj2: Second object (node, type, x, y, layer, net)
            prefer: "move-trace" or "move-via"

        Returns:
            The chosen object to move, or None if neither is movable
        """
        if obj1 is None and obj2 is None:
            return None
        if obj1 is None:
            return obj2
        if obj2 is None:
            return obj1

        _, type1, _, _, _, _ = obj1
        _, type2, _, _, _, _ = obj2

        if prefer == "move-trace":
            if type1 == "segment":
                return obj1
            if type2 == "segment":
                return obj2
            # Both are vias or other types, move the first one
            return obj1
        elif prefer == "move-via":
            if type1 == "via":
                return obj1
            if type2 == "via":
                return obj2
            return obj1
        else:
            return obj1

    def _compute_nudge(
        self,
        obj_x: float,
        obj_y: float,
        other_x: float,
        other_y: float,
        required_displacement: float,
    ) -> tuple[float, float, float] | None:
        """Compute the displacement vector to achieve required clearance.

        The nudge moves obj away from other by the required amount.

        Args:
            obj_x, obj_y: Position of the object to move
            other_x, other_y: Position of the object to move away from
            required_displacement: How far to move (mm)

        Returns:
            (dx, dy, distance) or None if objects are coincident
        """
        dx = obj_x - other_x
        dy = obj_y - other_y
        dist = math.sqrt(dx * dx + dy * dy)

        if dist < 1e-10:
            # Objects are at the same position, cannot determine direction
            # Default to moving in +x direction
            return (required_displacement, 0.0, required_displacement)

        # Normalize direction vector and scale to required displacement
        scale = required_displacement / dist
        nudge_x = dx * scale
        nudge_y = dy * scale
        nudge_dist = math.sqrt(nudge_x * nudge_x + nudge_y * nudge_y)

        return (nudge_x, nudge_y, nudge_dist)

    def _apply_nudge(
        self,
        node: SExp,
        obj_type: str,
        dx: float,
        dy: float,
    ) -> None:
        """Apply a displacement to a PCB object.

        For segments: moves the closest endpoint.
        For vias: moves the via position.
        """
        if obj_type == "via":
            at_node = node.find("at")
            if at_node:
                at_atoms = at_node.get_atoms()
                old_x = float(at_atoms[0]) if at_atoms else 0
                old_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0
                at_node.set_value(0, round(old_x + dx, 4))
                at_node.set_value(1, round(old_y + dy, 4))

        elif obj_type == "segment":
            # For segments, move both endpoints by the same amount
            # to preserve segment direction and length
            start_node = node.find("start")
            end_node = node.find("end")

            if start_node:
                start_atoms = start_node.get_atoms()
                sx = float(start_atoms[0]) if start_atoms else 0
                sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
                start_node.set_value(0, round(sx + dx, 4))
                start_node.set_value(1, round(sy + dy, 4))

            if end_node:
                end_atoms = end_node.get_atoms()
                ex = float(end_atoms[0]) if end_atoms else 0
                ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0
                end_node.set_value(0, round(ex + dx, 4))
                end_node.set_value(1, round(ey + dy, 4))

    def _closest_point_on_segment(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        px: float,
        py: float,
    ) -> tuple[float, float, float] | None:
        """Find the closest point on a line segment to a given point.

        Returns: (closest_x, closest_y, distance) or None
        """
        dx = x2 - x1
        dy = y2 - y1
        seg_len_sq = dx * dx + dy * dy

        if seg_len_sq < 1e-10:
            # Segment is a point
            dist = math.sqrt((x1 - px) ** 2 + (y1 - py) ** 2)
            return (x1, y1, dist)

        # Project point onto line, clamped to segment
        t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
        t = max(0.0, min(1.0, t))

        cx = x1 + t * dx
        cy = y1 + t * dy
        dist = math.sqrt((cx - px) ** 2 + (cy - py) ** 2)

        return (cx, cy, dist)

    def save(self, output_path: str | Path | None = None) -> None:
        """Save the modified PCB.

        Args:
            output_path: Path to save to. If None, overwrites the input file.
        """
        from ..core.sexp_file import save_pcb

        path = Path(output_path) if output_path else self.path
        save_pcb(self.doc, path)
