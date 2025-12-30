"""DRC violation fixer - automated repair of PCB issues.

This module provides tools to automatically fix DRC violations by:
1. Identifying the offending geometry (tracks, vias)
2. Deleting or modifying the problematic elements
3. Re-routing affected nets

Usage:
    from kicad_tools.drc import DRCReport
    from kicad_tools.drc.fixer import DRCFixer

    report = DRCReport.load("board-drc.rpt")
    fixer = DRCFixer("board.kicad_pcb")

    # Fix shorts by deleting offending traces
    fixed = fixer.fix_shorts(report)
    print(f"Fixed {fixed} shorts")

    fixer.save("board-fixed.kicad_pcb")
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..sexp import SExp, parse_file
from .report import DRCReport
from .violation import DRCViolation, Location, ViolationType


@dataclass
class TraceInfo:
    """Information about a track segment."""

    start_x: float
    start_y: float
    end_x: float
    end_y: float
    width: float
    layer: str
    net: int
    net_name: str
    uuid: str
    node: SExp  # Reference to the S-expression node


@dataclass
class ViaInfo:
    """Information about a via."""

    x: float
    y: float
    size: float
    drill: float
    net: int
    uuid: str
    node: SExp


class DRCFixer:
    """Automated DRC violation fixer."""

    def __init__(self, pcb_path: str):
        """Load a PCB file for fixing."""
        self.path = Path(pcb_path)
        self.doc = parse_file(self.path)
        self.deleted_count = 0
        self.modified = False

        # Build index of nets
        self.nets: dict[int, str] = {}
        self.net_names: dict[str, int] = {}
        self._parse_nets()

    def _parse_nets(self):
        """Parse net definitions."""
        for net_node in self.doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_num = int(atoms[0])
                net_name = str(atoms[1])
                self.nets[net_num] = net_name
                self.net_names[net_name] = net_num

    def find_segments_near(
        self,
        x: float,
        y: float,
        radius: float = 0.5,
        layer: Optional[str] = None,
        net_name: Optional[str] = None,
    ) -> list[TraceInfo]:
        """Find track segments within radius of a point."""
        segments = []

        for seg_node in self.doc.find_all("segment"):
            # Parse segment
            start_node = seg_node.find("start")
            end_node = seg_node.find("end")
            width_node = seg_node.find("width")
            layer_node = seg_node.find("layer")
            net_node = seg_node.find("net")
            uuid_node = seg_node.find("uuid")

            if not (start_node and end_node):
                continue

            start_atoms = start_node.get_atoms()
            end_atoms = end_node.get_atoms()

            sx = float(start_atoms[0]) if start_atoms else 0
            sy = float(start_atoms[1]) if len(start_atoms) > 1 else 0
            ex = float(end_atoms[0]) if end_atoms else 0
            ey = float(end_atoms[1]) if len(end_atoms) > 1 else 0

            # Check if segment passes near point
            if not self._segment_near_point(sx, sy, ex, ey, x, y, radius):
                continue

            seg_layer = layer_node.get_first_atom() if layer_node else ""
            if layer and seg_layer != layer:
                continue

            net_num = int(net_node.get_first_atom()) if net_node else 0
            seg_net_name = self.nets.get(net_num, "")

            if net_name and seg_net_name != net_name:
                continue

            width = float(width_node.get_first_atom()) if width_node else 0
            uuid_str = uuid_node.get_first_atom() if uuid_node else ""

            segments.append(TraceInfo(
                start_x=sx, start_y=sy,
                end_x=ex, end_y=ey,
                width=width,
                layer=seg_layer,
                net=net_num,
                net_name=seg_net_name,
                uuid=uuid_str,
                node=seg_node,
            ))

        return segments

    def find_vias_near(
        self,
        x: float,
        y: float,
        radius: float = 0.5,
        net_name: Optional[str] = None,
    ) -> list[ViaInfo]:
        """Find vias within radius of a point."""
        vias = []

        for via_node in self.doc.find_all("via"):
            at_node = via_node.find("at")
            if not at_node:
                continue

            at_atoms = at_node.get_atoms()
            vx = float(at_atoms[0]) if at_atoms else 0
            vy = float(at_atoms[1]) if len(at_atoms) > 1 else 0

            # Check distance
            dist = ((vx - x) ** 2 + (vy - y) ** 2) ** 0.5
            if dist > radius:
                continue

            net_node = via_node.find("net")
            net_num = int(net_node.get_first_atom()) if net_node else 0
            via_net_name = self.nets.get(net_num, "")

            if net_name and via_net_name != net_name:
                continue

            size_node = via_node.find("size")
            drill_node = via_node.find("drill")
            uuid_node = via_node.find("uuid")

            vias.append(ViaInfo(
                x=vx, y=vy,
                size=float(size_node.get_first_atom()) if size_node else 0,
                drill=float(drill_node.get_first_atom()) if drill_node else 0,
                net=net_num,
                uuid=uuid_node.get_first_atom() if uuid_node else "",
                node=via_node,
            ))

        return vias

    def _segment_near_point(
        self,
        x1: float, y1: float,
        x2: float, y2: float,
        px: float, py: float,
        radius: float,
    ) -> bool:
        """Check if a line segment passes within radius of a point."""
        # Vector from p1 to p2
        dx = x2 - x1
        dy = y2 - y1

        # If segment is a point
        seg_len_sq = dx * dx + dy * dy
        if seg_len_sq < 1e-10:
            dist = ((x1 - px) ** 2 + (y1 - py) ** 2) ** 0.5
            return dist <= radius

        # Project point onto line
        t = ((px - x1) * dx + (py - y1) * dy) / seg_len_sq
        t = max(0, min(1, t))  # Clamp to segment

        # Closest point on segment
        cx = x1 + t * dx
        cy = y1 + t * dy

        dist = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
        return dist <= radius

    def delete_segment(self, segment: TraceInfo) -> bool:
        """Delete a track segment from the PCB."""
        # Find and remove the node from parent
        for i, child in enumerate(self.doc.children):
            if child is segment.node:
                del self.doc.children[i]
                self.deleted_count += 1
                self.modified = True
                return True
        return False

    def delete_via(self, via: ViaInfo) -> bool:
        """Delete a via from the PCB."""
        for i, child in enumerate(self.doc.children):
            if child is via.node:
                del self.doc.children[i]
                self.deleted_count += 1
                self.modified = True
                return True
        return False

    def delete_net_traces(self, net_name: str) -> int:
        """Delete all traces and vias for a specific net."""
        net_num = self.net_names.get(net_name, 0)
        if net_num == 0:
            return 0

        deleted = 0

        # Collect nodes to delete (can't modify while iterating)
        to_delete = []

        for child in self.doc.children:
            # Skip atoms (non-list nodes)
            if child.is_atom:
                continue
            if child.name == "segment":
                net_node = child.find("net")
                if net_node and int(net_node.get_first_atom()) == net_num:
                    to_delete.append(child)
            elif child.name == "via":
                net_node = child.find("net")
                if net_node and int(net_node.get_first_atom()) == net_num:
                    to_delete.append(child)

        # Delete collected nodes
        for node in to_delete:
            self.doc.children.remove(node)
            deleted += 1

        if deleted > 0:
            self.modified = True
            self.deleted_count += deleted

        return deleted

    def fix_shorts(self, report: DRCReport, delete_both_nets: bool = False) -> int:
        """
        Fix short-circuit violations by deleting offending traces.

        Args:
            report: DRC report with violations
            delete_both_nets: If True, delete traces for both nets involved.
                             If False, only delete traces for the first net.

        Returns:
            Number of violations addressed
        """
        shorts = report.by_type(ViolationType.SHORTING_ITEMS)
        fixed = 0

        for violation in shorts:
            loc = violation.primary_location
            if not loc:
                continue

            # Find segments near the violation
            segments = self.find_segments_near(
                loc.x_mm, loc.y_mm,
                radius=1.0,  # 1mm radius
                layer=loc.layer if loc.layer else None,
            )

            # Delete segments that match the nets involved
            for seg in segments:
                if seg.net_name in violation.nets or not violation.nets:
                    self.delete_segment(seg)
                    fixed += 1

            # Also check for vias
            vias = self.find_vias_near(loc.x_mm, loc.y_mm, radius=1.0)
            for via in vias:
                via_net_name = self.nets.get(via.net, "")
                if via_net_name in violation.nets or not violation.nets:
                    self.delete_via(via)
                    fixed += 1

        return fixed

    def fix_clearance_violations(self, report: DRCReport) -> int:
        """
        Fix clearance violations by deleting traces that are too close.

        Note: This is a destructive fix - it removes traces that violate
        clearance rules. The net will need to be re-routed.
        """
        clearances = report.by_type(ViolationType.CLEARANCE)
        fixed = 0

        for violation in clearances:
            loc = violation.primary_location
            if not loc:
                continue

            # Find segments near the violation
            segments = self.find_segments_near(
                loc.x_mm, loc.y_mm,
                radius=0.5,
                layer=loc.layer if loc.layer else None,
            )

            # Delete the first segment found (one side of the clearance issue)
            for seg in segments[:1]:
                self.delete_segment(seg)
                fixed += 1

        return fixed

    def get_unconnected_nets(self, report: DRCReport) -> set[str]:
        """Get the set of nets that have unconnected items."""
        unconnected = report.by_type(ViolationType.UNCONNECTED_ITEMS)
        nets = set()
        for v in unconnected:
            nets.update(v.nets)
        return nets

    def get_affected_nets(self, report: DRCReport) -> set[str]:
        """Get all nets affected by any DRC violation."""
        nets = set()
        for v in report.violations:
            nets.update(v.nets)
        return nets

    def save(self, output_path: Optional[str] = None):
        """Save the modified PCB."""
        path = Path(output_path) if output_path else self.path
        path.write_text(self.doc.to_string() + "\n")

    def summary(self) -> str:
        """Generate a summary of fixes applied."""
        return f"DRC Fixer: deleted {self.deleted_count} elements"
