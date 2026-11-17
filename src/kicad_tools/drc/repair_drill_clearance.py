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
from typing import TYPE_CHECKING

from ..sexp import SExp, parse_file
from .net_compat import resolve_net_atom
from .violation import DRCViolation, ViolationType

if TYPE_CHECKING:
    from kicad_tools.manufacturers.base import DesignRules


@dataclass
class DrillRepairAction:
    """Record of a single drill clearance repair action.

    The signed ``displacement_x`` / ``displacement_y`` fields capture the
    direction of a slide so callers can reverse it (used by the granular
    rollback path in ``fix-drc``).  For ``deduplicate`` actions these are
    zero -- a removed via cannot currently be restored from the in-memory
    action record, so granular rollback falls back to a bulk snapshot
    restore when a dedup action is implicated.
    """

    action: str  # "deduplicate" or "slide"
    via_x: float
    via_y: float
    net_name: str
    detail: str  # human-readable description
    displacement_mm: float = 0.0  # 0 for dedup
    displacement_x: float = 0.0
    displacement_y: float = 0.0
    uuid: str = ""

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
    skipped_unsafe: int = 0
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
        if self.skipped_unsafe > 0:
            lines.append(
                f"  Skipped (slide would introduce a new violation): {self.skipped_unsafe}"
            )
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
        """Parse net definitions from the PCB.

        Only iterates top-level children of the PCB root node to avoid
        finding nested ``(net N)`` attribute nodes inside zones, segments,
        vias, and pads.  Top-level net definitions always have the form
        ``(net <number> "<name>")``.
        """
        for child in self.doc.children:
            if child.name != "net":
                continue
            atoms = child.get_atoms()
            if len(atoms) < 2:
                continue
            try:
                net_num = int(atoms[0])
            except (ValueError, TypeError):
                continue
            net_name = str(atoms[1])
            self.nets[net_num] = net_name
            self.net_names[net_name] = net_num

    def repair(
        self,
        violations: list[DRCViolation],
        max_displacement: float = 0.5,
        margin: float = 0.01,
        dry_run: bool = False,
        design_rules: DesignRules | None = None,
    ) -> DrillRepairResult:
        """Repair drill clearance violations.

        Args:
            violations: List of drill clearance violations
            max_displacement: Maximum allowed slide distance in mm
            margin: Extra clearance margin beyond minimum in mm
            dry_run: If True, compute repairs but don't modify PCB
            design_rules: Active manufacturer rules.  When provided, every
                computed slide is **re-validated** against the shared
                clearance engine
                (:func:`kicad_tools.cli.relocate_in_pad_vias._check_clearance`)
                before it is applied: a slide that would introduce a new
                hole-to-hole / copper-clearance violation (crowding another
                neighbour, shorting a foreign net) is **declined** -- the via
                is left in place and counted in
                :attr:`DrillRepairResult.skipped_unsafe`.  This is the
                clearance-safety gap #4017 hit: the previous ``_slide_via``
                moved a via without checking that its new position was itself
                legal, so the fix had to be done by hand.  When ``None``
                (default) the legacy unchecked-slide behaviour is preserved
                for backward compatibility.

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
            self._repair_single(violation, result, max_displacement, margin, dry_run, design_rules)

        return result

    def _repair_single(
        self,
        violation: DRCViolation,
        result: DrillRepairResult,
        max_displacement: float,
        margin: float,
        dry_run: bool,
        design_rules: DesignRules | None = None,
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
            design_rules,
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
            net_num, net_name = resolve_net_atom(
                net_node.get_first_atom() if net_node else None,
                self.nets,
                self.net_names,
            )

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
        uuid_node = via_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""
        action = DrillRepairAction(
            action="deduplicate",
            via_x=via_x,
            via_y=via_y,
            net_name=net_name,
            detail="removed duplicate same-net via",
            displacement_mm=0.0,
            uuid=str(uuid_str) if uuid_str else "",
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
        design_rules: DesignRules | None = None,
    ) -> None:
        """Slide a via away from another object to achieve clearance.

        Finds a connected trace segment and slides the via along it.
        Falls back to pushing via directly away if no connected trace exists.

        When ``design_rules`` is provided the computed target is re-validated
        against the shared clearance engine before it is applied; a slide that
        would introduce a new violation is declined (the via is left in place
        and counted in :attr:`DrillRepairResult.skipped_unsafe`).
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

        # Clearance-safety gate (#4408): re-validate the computed target before
        # applying it.  The legacy path slid ``dist`` and updated the segment
        # endpoint WITHOUT checking that the new position was itself legal, so a
        # slide could crowd the OTHER neighbour or short a foreign net -- which
        # is why #4017 had to be relocated by hand.  When ``design_rules`` is
        # provided we reject a target that introduces a new violation and leave
        # the via untouched (safety invariant: never make the board worse).
        if design_rules is not None:
            reason = self._target_clearance_reason(
                via_x, via_y, via_x + dx, via_y + dy, design_rules
            )
            if reason is not None:
                result.skipped_unsafe += 1
                return

        uuid_node = via_node.find("uuid")
        uuid_str = uuid_node.get_first_atom() if uuid_node else ""
        action = DrillRepairAction(
            action="slide",
            via_x=via_x,
            via_y=via_y,
            net_name=via_net,
            detail=f"slid {dist:.4f}mm to increase clearance",
            displacement_mm=dist,
            displacement_x=dx,
            displacement_y=dy,
            uuid=str(uuid_str) if uuid_str else "",
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

    def _target_clearance_reason(
        self,
        via_x: float,
        via_y: float,
        new_x: float,
        new_y: float,
        design_rules: DesignRules,
    ) -> str | None:
        """Return a reason string if moving the via at ``(via_x, via_y)`` to
        ``(new_x, new_y)`` would violate clearance; ``None`` when the target is
        clearance-safe.

        Bridges the SExp repair model to the shared PCB-schema clearance engine
        (:func:`kicad_tools.cli.relocate_in_pad_vias._check_clearance`) by
        building a lightweight :class:`PCB` view over the current document.
        Rebuilt per call because a prior repair may have mutated the document;
        drill-clearance violation clusters are small so this stays cheap.
        """
        from kicad_tools.cli.relocate_in_pad_vias import (
            _check_clearance,
            _collect_smd_pads_by_net,
            _collect_tht_pads,
        )
        from kicad_tools.schema.pcb import PCB

        pcb = PCB(self.doc)
        # The repairer works in ABSOLUTE file coordinates, but a PCB view is
        # board-relative (``_detect_board_origin``).  Convert the pre- and
        # post-move points into the view's frame before matching / checking.
        ox, oy = pcb._board_origin
        rel_via_x, rel_via_y = via_x - ox, via_y - oy
        rel_new_x, rel_new_y = new_x - ox, new_y - oy

        # Locate the schema Via matching the pre-move position so
        # ``_check_clearance`` exempts it (and reads its drill/size/net).
        target_via = None
        best = 1e-3
        for v in pcb.vias:
            d = math.hypot(v.position[0] - rel_via_x, v.position[1] - rel_via_y)
            if d < best:
                best = d
                target_via = v
        if target_via is None:
            # Could not map the via into the schema view; do not block the
            # legacy behaviour on a lookup miss.
            return None

        pads_by_net = _collect_smd_pads_by_net(pcb)
        tht_pads = _collect_tht_pads(pcb)
        return _check_clearance(
            pcb,
            target_via,
            rel_new_x,
            rel_new_y,
            pads_by_net,
            tht_pads,
            design_rules.min_clearance_mm,
            design_rules.min_hole_to_hole_mm,
        )

    def undo_action(self, action: DrillRepairAction) -> bool:
        """Reverse a previously-applied drill-clearance repair action.

        For ``slide`` actions, locates the via by UUID and applies the
        inverse displacement to both the via and any segment endpoint
        currently anchored at the slid (post-action) position.

        For ``deduplicate`` actions the original via was removed from the
        document, so the action record is insufficient to restore it.
        These return ``False`` so callers can fall back to a bulk-snapshot
        restore.

        Args:
            action: The :class:`DrillRepairAction` to undo.

        Returns:
            ``True`` on success, ``False`` when the action cannot be
            reversed (UUID lookup miss, dedup action, etc.).
        """
        if action.action == "deduplicate":
            # A removed via cannot be restored from the action record alone.
            # Signal failure so the orchestrator falls back to the bulk
            # snapshot restore.
            return False

        if action.action != "slide":
            return False

        uuid = action.uuid
        if not uuid:
            return False

        # Locate the via by UUID at its current (post-slide) position.
        target = None
        for via_node in self.doc.find_all("via"):
            uuid_node = via_node.find("uuid")
            if not uuid_node:
                continue
            atom = uuid_node.get_first_atom()
            if atom is None:
                continue
            if str(atom).strip('"') == str(uuid).strip('"'):
                target = via_node
                break
        if target is None:
            return False

        at_node = target.find("at")
        if not at_node:
            return False

        at_atoms = at_node.get_atoms()
        cur_x = float(at_atoms[0]) if at_atoms else 0.0
        cur_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0.0

        # The segment endpoint that was updated alongside the slide will
        # currently sit at the post-slide via position.  Reset it together
        # with the via.
        dx = -action.displacement_x
        dy = -action.displacement_y
        new_x = round(cur_x + dx, 4)
        new_y = round(cur_y + dy, 4)

        tolerance = 0.01
        segments_to_update: list[SExp] = []
        for seg_node in self.doc.find_all("segment"):
            start_node = seg_node.find("start")
            end_node = seg_node.find("end")
            if not (start_node and end_node):
                continue
            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()
            sx = float(start_atoms[0]) if start_atoms else 0.0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0.0
            ex = float(end_atoms[0]) if end_atoms else 0.0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0.0
            if (
                math.sqrt((sx - cur_x) ** 2 + (sy - cur_y) ** 2) < tolerance
                or math.sqrt((ex - cur_x) ** 2 + (ey - cur_y) ** 2) < tolerance
            ):
                segments_to_update.append(seg_node)

        at_node.set_value(0, new_x)
        at_node.set_value(1, new_y)

        for seg_node in segments_to_update:
            self._update_segment_endpoint(seg_node, cur_x, cur_y, new_x, new_y)

        return True

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
            seg_net, _ = resolve_net_atom(
                net_node.get_first_atom(),
                self.nets,
                self.net_names,
            )
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
