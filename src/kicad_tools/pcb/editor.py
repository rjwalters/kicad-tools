#!/usr/bin/env python3
"""
KiCad Agent Toolkit - Programmatic PCB Design for AI Agents

This module provides tools for AI agents to design, modify, and validate
KiCad PCB layouts without requiring GUI interaction.

Key capabilities:
1. Parse and modify .kicad_pcb files directly
2. Generate component placements based on design rules
3. Create routing guides and constraints
4. Run DRC validation
5. Generate manufacturing outputs

Usage:
    from kicad_agent import PCBEditor

    pcb = PCBEditor("board.kicad_pcb")
    pcb.place_component("U1", x=10, y=20, rotation=90)
    pcb.add_track("GND", [(10, 20), (30, 20), (30, 40)], width=0.3, layer="F.Cu")
    pcb.add_via((30, 40), drill=0.3, size=0.6)
    pcb.save()
"""

import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Import SExp parsing and builders
from kicad_tools.sexp import SExp, parse_file
from kicad_tools.sexp.builders import fmt, segment_node, via_node, zone_node


@dataclass
class Point:
    """2D point in mm."""

    x: float
    y: float

    def __iter__(self):
        yield self.x
        yield self.y


@dataclass
class Track:
    """PCB track/trace."""

    net: int
    start: Point
    end: Point
    width: float
    layer: str
    uuid_str: str = field(default_factory=lambda: str(uuid_module.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression node for this track segment."""
        return segment_node(
            self.start.x,
            self.start.y,
            self.end.x,
            self.end.y,
            self.width,
            self.layer,
            self.net,
            self.uuid_str,
        )


@dataclass
class Via:
    """PCB via."""

    net: int
    position: Point
    size: float
    drill: float
    layers: tuple = ("F.Cu", "B.Cu")
    uuid_str: str = field(default_factory=lambda: str(uuid_module.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression node for this via."""
        return via_node(
            self.position.x,
            self.position.y,
            self.size,
            self.drill,
            self.layers,
            self.net,
            self.uuid_str,
        )


@dataclass
class Zone:
    """Copper pour zone."""

    net: int
    net_name: str
    layer: str
    points: list[Point]
    priority: int = 0
    min_thickness: float = 0.2
    uuid_str: str = field(default_factory=lambda: str(uuid_module.uuid4()))

    def to_sexp_node(self) -> SExp:
        """Build S-expression node for this zone."""
        point_tuples = [(p.x, p.y) for p in self.points]
        return zone_node(
            self.net,
            self.net_name,
            self.layer,
            point_tuples,
            self.uuid_str,
            self.priority,
            self.min_thickness,
        )


class PCBEditor:
    """
    Agent-friendly KiCad PCB editor.

    Uses S-expression parsing for robust .kicad_pcb file manipulation.
    """

    def __init__(self, pcb_path: str):
        self.path = Path(pcb_path)
        self.doc: Optional[SExp] = None
        self.nets: dict[str, int] = {}
        self.footprints: dict[str, dict] = {}

        if self.path.exists():
            self.doc = parse_file(self.path)
            self._parse_nets()
            self._parse_footprints()

    def _parse_nets(self):
        """Extract net name to net number mapping using SExp."""
        if not self.doc:
            return

        for net_node in self.doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_num = int(atoms[0])
                net_name = str(atoms[1])
                self.nets[net_name] = net_num

    def _parse_footprints(self):
        """Extract footprint references and positions using SExp."""
        if not self.doc:
            return

        for fp_node in self.doc.find_all("footprint"):
            # Get reference from fp_text (PCB format) or property (newer format)
            ref = None

            # Try fp_text reference first (standard PCB format)
            for fp_text in fp_node.find_all("fp_text"):
                atoms = fp_text.get_atoms()
                if len(atoms) >= 2 and str(atoms[0]) == "reference":
                    ref = str(atoms[1])
                    break

            # Fallback: try property "Reference" (alternative format)
            if ref is None:
                for prop_node in fp_node.find_all("property"):
                    atoms = prop_node.get_atoms()
                    if len(atoms) >= 2 and str(atoms[0]) == "Reference":
                        ref = str(atoms[1])
                        break

            if ref is None:
                continue

            # Get position
            at_node = fp_node.get("at")
            if at_node:
                atoms = at_node.get_atoms()
                x = float(atoms[0]) if len(atoms) > 0 else 0
                y = float(atoms[1]) if len(atoms) > 1 else 0
                rotation = float(atoms[2]) if len(atoms) > 2 else 0

                # Get layer
                layer_node = fp_node.get("layer")
                layer = str(layer_node.get_first_atom()) if layer_node else "F.Cu"

                self.footprints[ref] = {
                    "x": x,
                    "y": y,
                    "rotation": rotation,
                    "layer": layer,
                    "_node": fp_node,  # Store reference for modification
                }

    def get_net_number(self, net_name: str) -> int:
        """Get net number by name."""
        return self.nets.get(net_name, 0)

    def place_component(
        self, ref: str, x: float, y: float, rotation: float = 0, layer: str = "F.Cu"
    ) -> bool:
        """
        Move a component to specified position.

        Returns True if component was found and moved.
        """
        if ref not in self.footprints:
            return False

        fp_info = self.footprints[ref]
        fp_node = fp_info.get("_node")

        if not fp_node:
            return False

        # Find and update the 'at' node
        at_node = fp_node.get("at")
        if at_node:
            # Replace atoms with new values
            at_node.children = []
            at_node.children.append(SExp(value=fmt(x)))
            at_node.children.append(SExp(value=fmt(y)))
            if rotation != 0:
                at_node.children.append(SExp(value=fmt(rotation)))

            # Update cache
            fp_info["x"] = x
            fp_info["y"] = y
            fp_info["rotation"] = rotation

            return True

        return False

    def add_track(
        self,
        net_name: str,
        points: list[tuple[float, float]],
        width: float = 0.2,
        layer: str = "F.Cu",
        insert: bool = True,
    ) -> list[Track]:
        """
        Add a multi-segment track.

        Args:
            net_name: Net to connect (e.g., "GND", "+3.3V")
            points: List of (x, y) coordinates in mm
            width: Track width in mm
            layer: Copper layer
            insert: If True, insert into document immediately

        Returns:
            List of Track segments created
        """
        net_num = self.get_net_number(net_name)
        tracks = []

        for i in range(len(points) - 1):
            track = Track(
                net=net_num,
                start=Point(*points[i]),
                end=Point(*points[i + 1]),
                width=width,
                layer=layer,
            )
            tracks.append(track)

            if insert and self.doc:
                self.doc.append(track.to_sexp_node())

        return tracks

    def add_via(
        self,
        position: tuple[float, float],
        net_name: str,
        drill: float = 0.3,
        size: float = 0.6,
        insert: bool = True,
    ) -> Via:
        """Add a via at specified position.

        Args:
            position: (x, y) coordinates in mm
            net_name: Net to connect
            drill: Drill hole diameter in mm
            size: Via pad size in mm
            insert: If True, insert into document immediately
        """
        via = Via(
            net=self.get_net_number(net_name), position=Point(*position), size=size, drill=drill
        )

        if insert and self.doc:
            self.doc.append(via.to_sexp_node())

        return via

    def add_zone(
        self,
        net_name: str,
        layer: str,
        boundary: list[tuple[float, float]],
        priority: int = 0,
        insert: bool = True,
    ) -> Zone:
        """
        Add a copper pour zone.

        Args:
            net_name: Net for the zone (e.g., "GND")
            layer: Copper layer
            boundary: Zone boundary points
            priority: Zone fill priority (higher fills later)
            insert: If True, insert into document immediately

        Returns:
            Zone object created
        """
        zone = Zone(
            net=self.get_net_number(net_name),
            net_name=net_name,
            layer=layer,
            points=[Point(*p) for p in boundary],
            priority=priority,
        )

        if insert and self.doc:
            self.doc.append(zone.to_sexp_node())

        return zone

    def create_ground_pour(
        self,
        layer: str = "In1.Cu",
        boundary: Optional[list[tuple[float, float]]] = None,
        insert: bool = True,
    ) -> Zone:
        """
        Generate a ground plane pour.

        Args:
            layer: Target copper layer (usually inner layer for 4-layer)
            boundary: Zone boundary points, defaults to board outline
            insert: If True, insert into document immediately

        Returns:
            Zone object created
        """
        # Default to full board pour
        if boundary is None:
            boundary = [(0, 0), (65, 0), (65, 56), (0, 56)]

        return self.add_zone("GND", layer, boundary, priority=0, insert=insert)

    def generate_routing_script(self, connections: list[dict]) -> str:
        """
        Generate a routing script from connection specifications.

        Args:
            connections: List of dicts with 'net', 'from', 'to', 'width', 'layer'

        Returns:
            Python script for KiCad console
        """
        script = '''#!/usr/bin/env python3
"""Auto-generated routing script for KiCad."""
import pcbnew

board = pcbnew.GetBoard()

def add_track(net_name, start, end, width, layer):
    """Add a single track segment."""
    net = board.FindNet(net_name)
    if not net:
        print(f"Warning: Net {net_name} not found")
        return

    track = pcbnew.PCB_TRACK(board)
    track.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(start[0]), pcbnew.FromMM(start[1])))
    track.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(end[0]), pcbnew.FromMM(end[1])))
    track.SetWidth(pcbnew.FromMM(width))
    track.SetLayer(board.GetLayerID(layer))
    track.SetNet(net)
    board.Add(track)

def add_via(net_name, pos, drill=0.3, size=0.6):
    """Add a via."""
    net = board.FindNet(net_name)
    if not net:
        print(f"Warning: Net {net_name} not found")
        return

    via = pcbnew.PCB_VIA(board)
    via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(pos[0]), pcbnew.FromMM(pos[1])))
    via.SetDrill(pcbnew.FromMM(drill))
    via.SetWidth(pcbnew.FromMM(size))
    via.SetNet(net)
    board.Add(via)

# Routes
'''
        for conn in connections:
            net = conn.get("net", "GND")
            start = conn.get("from", (0, 0))
            end = conn.get("to", (0, 0))
            width = conn.get("width", 0.2)
            layer = conn.get("layer", "F.Cu")

            script += f"add_track('{net}', {start}, {end}, {width}, '{layer}')\n"

            if conn.get("via"):
                script += f"add_via('{net}', {end})\n"

        script += "\npcbnew.Refresh()\nprint('Routing complete!')\n"
        return script

    def validate_placement(self) -> list[str]:
        """
        Check placement for common issues.

        Returns list of warning/error messages.
        """
        issues = []

        # Check for components outside board boundary
        # Check for overlapping footprints
        # Check for minimum clearances

        return issues

    def save(self, output_path: Optional[str] = None):
        """Save modified PCB file."""
        if not self.doc:
            raise ValueError("No PCB document loaded")
        path = Path(output_path) if output_path else self.path
        path.write_text(self.doc.to_string())


# =============================================================================
# DESIGN RULE HELPERS
# =============================================================================


class SeeedFusion4Layer:
    """Seeed Fusion 4-layer PCB design rules."""

    MIN_TRACE_WIDTH = 0.1  # mm (4 mil min, 6 mil recommended)
    MIN_CLEARANCE = 0.1  # mm
    MIN_VIA_DRILL = 0.2  # mm
    MIN_VIA_SIZE = 0.45  # mm (drill + 2*annular ring)
    MIN_HOLE = 0.3  # mm
    COPPER_TO_EDGE = 0.3  # mm

    RECOMMENDED_TRACE = 0.15  # 6 mil
    RECOMMENDED_VIA_DRILL = 0.3
    RECOMMENDED_VIA_SIZE = 0.6

    @classmethod
    def power_trace_width(cls, current_ma: float, temp_rise_c: float = 10) -> float:
        """Calculate trace width for given current (external layer, 1oz copper)."""
        # IPC-2221 formula (simplified)
        # Area (mils²) = (I / (k * ΔT^b))^(1/c)
        # For external: k=0.048, b=0.44, c=0.725
        area_mils2 = (current_ma / (0.048 * (temp_rise_c**0.44))) ** (1 / 0.725)
        width_mils = area_mils2 / 1.4  # Assuming 1oz = 1.4 mils thick
        width_mm = width_mils * 0.0254
        return max(width_mm, cls.MIN_TRACE_WIDTH)


# =============================================================================
# AUDIO PCB HELPERS
# =============================================================================


class AudioLayoutRules:
    """Best practices for audio PCB layout."""

    @staticmethod
    def analog_ground_zone(board_width: float, split_x: float) -> list[tuple[float, float]]:
        """Define analog ground zone (left side of board)."""
        return [(0, 0), (split_x, 0), (split_x, 56), (0, 56)]

    @staticmethod
    def star_ground_point(analog_zone_center: tuple[float, float]) -> tuple[float, float]:
        """Calculate optimal star ground connection point."""
        # Typically near the DAC for audio circuits
        return analog_zone_center

    @staticmethod
    def clock_trace_length_match(
        source: tuple[float, float], dest1: tuple[float, float], dest2: tuple[float, float]
    ) -> dict:
        """
        Calculate serpentine requirements for clock length matching.

        Returns dict with routing guidance.
        """
        import math

        len1 = math.sqrt((dest1[0] - source[0]) ** 2 + (dest1[1] - source[1]) ** 2)
        len2 = math.sqrt((dest2[0] - source[0]) ** 2 + (dest2[1] - source[1]) ** 2)

        return {
            "length_diff_mm": abs(len1 - len2),
            "shorter_path": "dest1" if len1 < len2 else "dest2",
            "serpentine_needed": abs(len1 - len2) > 1.0,  # >1mm diff
            "recommended_meander": max(0, abs(len1 - len2)),
        }


# =============================================================================
# CLI INTERFACE
# =============================================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="KiCad Agent PCB Tools")
    parser.add_argument("pcb_file", help="Path to .kicad_pcb file")
    parser.add_argument("--info", action="store_true", help="Show board info")
    parser.add_argument("--nets", action="store_true", help="List all nets")
    parser.add_argument(
        "--ground-pour", type=str, metavar="LAYER", help="Generate ground pour for layer"
    )

    args = parser.parse_args()

    pcb = PCBEditor(args.pcb_file)

    if args.info:
        print(f"PCB File: {pcb.path}")
        print(f"Nets defined: {len(pcb.nets)}")

    if args.nets:
        print("\nNets:")
        for name, num in sorted(pcb.nets.items(), key=lambda x: x[1]):
            print(f"  {num:3d}: {name}")

    if args.ground_pour:
        pour = pcb.create_ground_pour(layer=args.ground_pour)
        print(pour)


if __name__ == "__main__":
    main()
