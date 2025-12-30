"""
PCB State Extraction - Structured representation of board state for LLM reasoning.

This module extracts and represents PCB state in a form that:
1. Is human-readable (for LLM consumption)
2. Captures spatial relationships qualitatively
3. Tracks routing progress and violations
4. Enables comparison between states
"""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from ..sexp import SExp, parse_file
from ..drc.report import DRCReport
from ..drc.violation import ViolationType


@dataclass
class PadState:
    """State of a single pad."""

    ref: str  # Component reference (e.g., "U1")
    number: str  # Pad number (e.g., "1", "A1")
    x: float  # Position in mm
    y: float  # Position in mm
    net: str  # Net name
    net_id: int  # Net number
    layer: str  # Layer (F.Cu, B.Cu, or through-hole)
    width: float  # Pad width
    height: float  # Pad height
    through_hole: bool = False

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    def distance_to(self, other: "PadState") -> float:
        """Manhattan distance to another pad."""
        return abs(self.x - other.x) + abs(self.y - other.y)


@dataclass
class ComponentState:
    """State of a placed component."""

    ref: str  # Reference designator
    footprint: str  # Footprint library:name
    x: float  # Center X in mm
    y: float  # Center Y in mm
    rotation: float  # Rotation in degrees
    layer: str  # F.Cu or B.Cu (component side)
    pads: list[PadState] = field(default_factory=list)
    value: str = ""
    fixed: bool = False  # True if component cannot be moved

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Approximate bounding box (x1, y1, x2, y2)."""
        if not self.pads:
            return (self.x - 1, self.y - 1, self.x + 1, self.y + 1)
        xs = [p.x for p in self.pads]
        ys = [p.y for p in self.pads]
        margin = 0.5
        return (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)


@dataclass
class TraceState:
    """State of a trace segment."""

    net: str
    net_id: int
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    uuid: str = ""

    @property
    def length(self) -> float:
        """Euclidean length in mm."""
        return ((self.x2 - self.x1) ** 2 + (self.y2 - self.y1) ** 2) ** 0.5

    @property
    def start(self) -> tuple[float, float]:
        return (self.x1, self.y1)

    @property
    def end(self) -> tuple[float, float]:
        return (self.x2, self.y2)


@dataclass
class ViaState:
    """State of a via."""

    net: str
    net_id: int
    x: float
    y: float
    size: float
    drill: float
    layers: tuple[str, str] = ("F.Cu", "B.Cu")
    uuid: str = ""

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class ZoneState:
    """State of a copper zone/pour."""

    net: str
    net_id: int
    layer: str
    priority: int
    bounds: tuple[float, float, float, float]  # Bounding box
    filled: bool = True


@dataclass
class ViolationState:
    """State of a DRC violation."""

    type: str  # ViolationType string
    severity: str  # "error" or "warning"
    message: str
    x: float
    y: float
    layer: str = ""
    nets: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return self.severity == "error"

    @property
    def position(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class NetState:
    """State of a net (electrical connection)."""

    name: str
    net_id: int
    pads: list[tuple[str, str]] = field(default_factory=list)  # (ref, pad_number)
    traces: list[TraceState] = field(default_factory=list)
    vias: list[ViaState] = field(default_factory=list)
    is_power: bool = False
    is_ground: bool = False
    is_clock: bool = False
    priority: int = 10  # Lower = higher priority

    @property
    def pad_count(self) -> int:
        return len(self.pads)

    @property
    def is_routed(self) -> bool:
        """True if net has any traces."""
        return len(self.traces) > 0

    @property
    def total_trace_length(self) -> float:
        """Total length of all traces in mm."""
        return sum(t.length for t in self.traces)


@dataclass
class BoardOutline:
    """Board outline polygon."""

    points: list[tuple[float, float]]
    width: float = 0.0
    height: float = 0.0
    center_x: float = 0.0
    center_y: float = 0.0

    @classmethod
    def from_points(cls, points: list[tuple[float, float]]) -> "BoardOutline":
        """Create from list of points, computing dimensions."""
        if not points:
            return cls(points=[])
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        return cls(
            points=points,
            width=max_x - min_x,
            height=max_y - min_y,
            center_x=(min_x + max_x) / 2,
            center_y=(min_y + max_y) / 2,
        )


@dataclass
class PCBState:
    """Complete state of a PCB for reasoning.

    This is the primary interface between the LLM and the PCB.
    It provides a structured, human-readable representation of:
    - Board geometry
    - Component placement
    - Net connections
    - Routing progress
    - DRC violations
    """

    # Board geometry
    outline: BoardOutline
    layers: list[str]

    # Components
    components: dict[str, ComponentState]

    # Nets
    nets: dict[str, NetState]

    # Routing
    traces: list[TraceState]
    vias: list[ViaState]
    zones: list[ZoneState]

    # Violations
    violations: list[ViolationState]

    # Metadata
    source_file: str = ""
    design_rules: dict = field(default_factory=dict)

    # =========================================================================
    # Factory Methods
    # =========================================================================

    @classmethod
    def from_pcb(
        cls, pcb_path: str | Path, drc_report: Optional[DRCReport] = None
    ) -> "PCBState":
        """Load state from a KiCad PCB file."""
        path = Path(pcb_path)
        doc = parse_file(path)
        return cls._parse_pcb(doc, str(path), drc_report)

    @classmethod
    def _parse_pcb(
        cls, doc: SExp, source_file: str, drc_report: Optional[DRCReport]
    ) -> "PCBState":
        """Parse PCB document into state."""
        # Parse nets
        net_map: dict[int, str] = {}
        nets: dict[str, NetState] = {}

        for net_node in doc.find_all("net"):
            atoms = net_node.get_atoms()
            if len(atoms) >= 2:
                net_id = int(atoms[0])
                net_name = str(atoms[1])
                net_map[net_id] = net_name

                # Classify net type
                name_lower = net_name.lower()
                is_power = any(
                    p in name_lower for p in ["+", "vcc", "vdd", "3v3", "5v", "12v"]
                )
                is_ground = any(g in name_lower for g in ["gnd", "vss", "ground"])
                is_clock = any(
                    c in name_lower for c in ["clk", "clock", "mclk", "bclk", "lrclk"]
                )

                # Set priority based on type
                if is_ground:
                    priority = 1  # Ground first
                elif is_power:
                    priority = 2  # Then power
                elif is_clock:
                    priority = 3  # Then clocks
                else:
                    priority = 10  # Everything else

                nets[net_name] = NetState(
                    name=net_name,
                    net_id=net_id,
                    is_power=is_power,
                    is_ground=is_ground,
                    is_clock=is_clock,
                    priority=priority,
                )

        # Parse layers
        layers = []
        for layer_node in doc.find_all("layer"):
            atoms = layer_node.get_atoms()
            if len(atoms) >= 2:
                layer_name = str(atoms[1])
                if "Cu" in layer_name:
                    layers.append(layer_name)

        if not layers:
            layers = ["F.Cu", "B.Cu"]  # Default 2-layer

        # Parse components and pads
        components: dict[str, ComponentState] = {}

        for fp_node in doc.find_all("footprint"):
            comp = cls._parse_footprint(fp_node, net_map, nets)
            if comp:
                components[comp.ref] = comp

        # Parse traces
        traces: list[TraceState] = []
        for seg_node in doc.find_all("segment"):
            trace = cls._parse_segment(seg_node, net_map)
            if trace:
                traces.append(trace)
                # Add to net
                if trace.net in nets:
                    nets[trace.net].traces.append(trace)

        # Parse vias
        vias: list[ViaState] = []
        for via_node in doc.find_all("via"):
            via = cls._parse_via(via_node, net_map)
            if via:
                vias.append(via)
                if via.net in nets:
                    nets[via.net].vias.append(via)

        # Parse zones
        zones: list[ZoneState] = []
        for zone_node in doc.find_all("zone"):
            zone = cls._parse_zone(zone_node, net_map)
            if zone:
                zones.append(zone)

        # Parse board outline
        outline = cls._parse_outline(doc)

        # Parse violations from DRC report
        violations: list[ViolationState] = []
        if drc_report:
            for v in drc_report.violations:
                loc = v.primary_location
                violations.append(
                    ViolationState(
                        type=v.type.value,
                        severity="error" if v.is_error else "warning",
                        message=v.message,
                        x=loc.x_mm if loc else 0,
                        y=loc.y_mm if loc else 0,
                        layer=loc.layer if loc else "",
                        nets=v.nets.copy(),
                        items=v.items.copy(),
                    )
                )

        return cls(
            outline=outline,
            layers=layers,
            components=components,
            nets=nets,
            traces=traces,
            vias=vias,
            zones=zones,
            violations=violations,
            source_file=source_file,
        )

    @classmethod
    def _parse_footprint(
        cls, fp_node: SExp, net_map: dict[int, str], nets: dict[str, NetState]
    ) -> Optional[ComponentState]:
        """Parse a footprint node."""
        # Get reference
        ref = None
        value = ""

        for fp_text in fp_node.find_all("fp_text"):
            atoms = fp_text.get_atoms()
            if len(atoms) >= 2:
                text_type = str(atoms[0])
                text_value = str(atoms[1])
                if text_type == "reference":
                    ref = text_value
                elif text_type == "value":
                    value = text_value

        # Try property nodes (newer format)
        if ref is None:
            for prop in fp_node.find_all("property"):
                atoms = prop.get_atoms()
                if len(atoms) >= 2 and str(atoms[0]) == "Reference":
                    ref = str(atoms[1])
                    break

        if ref is None:
            return None

        # Get position
        at_node = fp_node.get("at")
        x, y, rotation = 0.0, 0.0, 0.0
        if at_node:
            atoms = at_node.get_atoms()
            x = float(atoms[0]) if len(atoms) > 0 else 0
            y = float(atoms[1]) if len(atoms) > 1 else 0
            rotation = float(atoms[2]) if len(atoms) > 2 else 0

        # Get layer
        layer_node = fp_node.get("layer")
        layer = str(layer_node.get_first_atom()) if layer_node else "F.Cu"

        # Get footprint name
        fp_name = str(fp_node.get_first_atom()) if fp_node.get_atoms() else ""

        # Parse pads
        pads: list[PadState] = []
        import math

        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad_node in fp_node.find_all("pad"):
            pad = cls._parse_pad(pad_node, ref, x, y, cos_r, sin_r, net_map, nets)
            if pad:
                pads.append(pad)

        return ComponentState(
            ref=ref,
            footprint=fp_name,
            x=x,
            y=y,
            rotation=rotation,
            layer=layer,
            pads=pads,
            value=value,
        )

    @classmethod
    def _parse_pad(
        cls,
        pad_node: SExp,
        ref: str,
        comp_x: float,
        comp_y: float,
        cos_r: float,
        sin_r: float,
        net_map: dict[int, str],
        nets: dict[str, NetState],
    ) -> Optional[PadState]:
        """Parse a pad node."""
        atoms = pad_node.get_atoms()
        if len(atoms) < 2:
            return None

        pad_num = str(atoms[0])
        pad_type = str(atoms[1])

        # Get position relative to component
        at_node = pad_node.find("at")
        if not at_node:
            return None

        at_atoms = at_node.get_atoms()
        rel_x = float(at_atoms[0]) if len(at_atoms) > 0 else 0
        rel_y = float(at_atoms[1]) if len(at_atoms) > 1 else 0

        # Rotate to absolute position
        abs_x = comp_x + (rel_x * cos_r - rel_y * sin_r)
        abs_y = comp_y + (rel_x * sin_r + rel_y * cos_r)

        # Get size
        size_node = pad_node.find("size")
        width, height = 0.5, 0.5
        if size_node:
            size_atoms = size_node.get_atoms()
            width = float(size_atoms[0]) if len(size_atoms) > 0 else 0.5
            height = float(size_atoms[1]) if len(size_atoms) > 1 else width

        # Get net
        net_node = pad_node.find("net")
        net_id = 0
        net_name = ""
        if net_node:
            net_atoms = net_node.get_atoms()
            net_id = int(net_atoms[0]) if len(net_atoms) > 0 else 0
            net_name = net_map.get(net_id, "")

        # Get layer(s)
        layer = "F.Cu"
        layers_node = pad_node.find("layers")
        through_hole = False
        if layers_node:
            layer_atoms = layers_node.get_atoms()
            if layer_atoms:
                layer = str(layer_atoms[0])
            if any("*.Cu" in str(a) for a in layer_atoms):
                through_hole = True

        if pad_type == "thru_hole":
            through_hole = True

        # Add pad to net
        if net_name and net_name in nets:
            nets[net_name].pads.append((ref, pad_num))

        return PadState(
            ref=ref,
            number=pad_num,
            x=abs_x,
            y=abs_y,
            net=net_name,
            net_id=net_id,
            layer=layer,
            width=width,
            height=height,
            through_hole=through_hole,
        )

    @classmethod
    def _parse_segment(cls, seg_node: SExp, net_map: dict[int, str]) -> Optional[TraceState]:
        """Parse a segment (trace) node."""
        start_node = seg_node.find("start")
        end_node = seg_node.find("end")
        if not (start_node and end_node):
            return None

        start_atoms = start_node.get_atoms()
        end_atoms = end_node.get_atoms()

        x1 = float(start_atoms[0]) if len(start_atoms) > 0 else 0
        y1 = float(start_atoms[1]) if len(start_atoms) > 1 else 0
        x2 = float(end_atoms[0]) if len(end_atoms) > 0 else 0
        y2 = float(end_atoms[1]) if len(end_atoms) > 1 else 0

        width_node = seg_node.find("width")
        width = float(width_node.get_first_atom()) if width_node else 0.2

        layer_node = seg_node.find("layer")
        layer = str(layer_node.get_first_atom()) if layer_node else "F.Cu"

        net_node = seg_node.find("net")
        net_id = int(net_node.get_first_atom()) if net_node else 0
        net_name = net_map.get(net_id, "")

        uuid_node = seg_node.find("uuid")
        uuid = str(uuid_node.get_first_atom()) if uuid_node else ""

        return TraceState(
            net=net_name,
            net_id=net_id,
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=width,
            layer=layer,
            uuid=uuid,
        )

    @classmethod
    def _parse_via(cls, via_node: SExp, net_map: dict[int, str]) -> Optional[ViaState]:
        """Parse a via node."""
        at_node = via_node.find("at")
        if not at_node:
            return None

        at_atoms = at_node.get_atoms()
        x = float(at_atoms[0]) if len(at_atoms) > 0 else 0
        y = float(at_atoms[1]) if len(at_atoms) > 1 else 0

        size_node = via_node.find("size")
        size = float(size_node.get_first_atom()) if size_node else 0.6

        drill_node = via_node.find("drill")
        drill = float(drill_node.get_first_atom()) if drill_node else 0.3

        net_node = via_node.find("net")
        net_id = int(net_node.get_first_atom()) if net_node else 0
        net_name = net_map.get(net_id, "")

        layers_node = via_node.find("layers")
        layers = ("F.Cu", "B.Cu")
        if layers_node:
            layer_atoms = layers_node.get_atoms()
            if len(layer_atoms) >= 2:
                layers = (str(layer_atoms[0]), str(layer_atoms[1]))

        uuid_node = via_node.find("uuid")
        uuid = str(uuid_node.get_first_atom()) if uuid_node else ""

        return ViaState(
            net=net_name,
            net_id=net_id,
            x=x,
            y=y,
            size=size,
            drill=drill,
            layers=layers,
            uuid=uuid,
        )

    @classmethod
    def _parse_zone(cls, zone_node: SExp, net_map: dict[int, str]) -> Optional[ZoneState]:
        """Parse a zone node."""
        net_node = zone_node.find("net")
        net_id = int(net_node.get_first_atom()) if net_node else 0
        net_name = net_map.get(net_id, "")

        layer_node = zone_node.find("layer")
        layer = str(layer_node.get_first_atom()) if layer_node else "F.Cu"

        priority_node = zone_node.find("priority")
        priority = int(priority_node.get_first_atom()) if priority_node else 0

        # Get bounding box from polygon points
        bounds = (0.0, 0.0, 0.0, 0.0)
        polygon = zone_node.find("polygon")
        if polygon:
            pts = polygon.find("pts")
            if pts:
                xs, ys = [], []
                for xy in pts.find_all("xy"):
                    atoms = xy.get_atoms()
                    if len(atoms) >= 2:
                        xs.append(float(atoms[0]))
                        ys.append(float(atoms[1]))
                if xs and ys:
                    bounds = (min(xs), min(ys), max(xs), max(ys))

        return ZoneState(
            net=net_name,
            net_id=net_id,
            layer=layer,
            priority=priority,
            bounds=bounds,
        )

    @classmethod
    def _parse_outline(cls, doc: SExp) -> BoardOutline:
        """Parse board outline from Edge.Cuts layer."""
        points = []

        for gr_line in doc.find_all("gr_line"):
            layer_node = gr_line.find("layer")
            if layer_node and str(layer_node.get_first_atom()) == "Edge.Cuts":
                start = gr_line.find("start")
                end = gr_line.find("end")
                if start and end:
                    s_atoms = start.get_atoms()
                    e_atoms = end.get_atoms()
                    if len(s_atoms) >= 2:
                        points.append((float(s_atoms[0]), float(s_atoms[1])))
                    if len(e_atoms) >= 2:
                        points.append((float(e_atoms[0]), float(e_atoms[1])))

        # Deduplicate and sort
        unique_points = list(set(points))

        return BoardOutline.from_points(unique_points)

    # =========================================================================
    # Query Methods
    # =========================================================================

    def get_component(self, ref: str) -> Optional[ComponentState]:
        """Get component by reference."""
        return self.components.get(ref)

    def get_net(self, name: str) -> Optional[NetState]:
        """Get net by name."""
        return self.nets.get(name)

    def get_pad(self, ref: str, pad_num: str) -> Optional[PadState]:
        """Get a specific pad."""
        comp = self.components.get(ref)
        if comp:
            for pad in comp.pads:
                if pad.number == pad_num:
                    return pad
        return None

    def components_near(
        self, x: float, y: float, radius: float = 10.0
    ) -> list[ComponentState]:
        """Find components within radius of a point."""
        result = []
        for comp in self.components.values():
            dist = abs(comp.x - x) + abs(comp.y - y)
            if dist <= radius:
                result.append(comp)
        return result

    def violations_near(
        self, x: float, y: float, radius: float = 5.0
    ) -> list[ViolationState]:
        """Find violations within radius of a point."""
        result = []
        for v in self.violations:
            dist = ((v.x - x) ** 2 + (v.y - y) ** 2) ** 0.5
            if dist <= radius:
                result.append(v)
        return result

    # =========================================================================
    # Statistics
    # =========================================================================

    @property
    def unrouted_nets(self) -> list[NetState]:
        """Get nets that have no traces."""
        return [n for n in self.nets.values() if not n.is_routed and n.pad_count >= 2]

    @property
    def routed_nets(self) -> list[NetState]:
        """Get nets that have traces."""
        return [n for n in self.nets.values() if n.is_routed]

    @property
    def shorts(self) -> list[ViolationState]:
        """Get short-circuit violations."""
        return [v for v in self.violations if v.type == "shorting_items"]

    @property
    def clearance_violations(self) -> list[ViolationState]:
        """Get clearance violations."""
        return [v for v in self.violations if v.type == "clearance"]

    @property
    def unconnected_violations(self) -> list[ViolationState]:
        """Get unconnected item violations."""
        return [v for v in self.violations if v.type == "unconnected_items"]

    def summary(self) -> dict:
        """Generate summary statistics."""
        return {
            "board_size": f"{self.outline.width:.1f} x {self.outline.height:.1f} mm",
            "layers": len(self.layers),
            "components": len(self.components),
            "nets_total": len(self.nets),
            "nets_routed": len(self.routed_nets),
            "nets_unrouted": len(self.unrouted_nets),
            "traces": len(self.traces),
            "vias": len(self.vias),
            "zones": len(self.zones),
            "violations_total": len(self.violations),
            "shorts": len(self.shorts),
            "clearance_violations": len(self.clearance_violations),
            "unconnected": len(self.unconnected_violations),
        }

    # =========================================================================
    # LLM Interface
    # =========================================================================

    def to_prompt(self, include_violations: bool = True, max_items: int = 50) -> str:
        """Generate a prompt-friendly representation of the state.

        This is the primary interface for LLM consumption.
        """
        lines = []

        # Board overview
        lines.append("## PCB State")
        lines.append("")
        lines.append(f"Board: {self.outline.width:.1f} x {self.outline.height:.1f} mm")
        lines.append(f"Layers: {', '.join(self.layers)}")
        lines.append(f"Components: {len(self.components)}")
        lines.append("")

        # Routing progress
        lines.append("## Routing Progress")
        lines.append("")
        routed = len(self.routed_nets)
        unrouted = len(self.unrouted_nets)
        total = routed + unrouted
        lines.append(f"Nets routed: {routed}/{total}")
        lines.append(f"Traces: {len(self.traces)}")
        lines.append(f"Vias: {len(self.vias)}")
        lines.append("")

        # Unrouted nets (most important for routing)
        if self.unrouted_nets:
            lines.append("## Unrouted Nets (by priority)")
            lines.append("")
            sorted_unrouted = sorted(self.unrouted_nets, key=lambda n: n.priority)
            for net in sorted_unrouted[:max_items]:
                net_type = ""
                if net.is_power:
                    net_type = " [POWER]"
                elif net.is_ground:
                    net_type = " [GND]"
                elif net.is_clock:
                    net_type = " [CLOCK]"
                lines.append(f"- {net.name}{net_type}: {net.pad_count} pads")
            if len(sorted_unrouted) > max_items:
                lines.append(f"  ... and {len(sorted_unrouted) - max_items} more")
            lines.append("")

        # Violations
        if include_violations and self.violations:
            lines.append("## DRC Violations")
            lines.append("")

            # Group by type
            by_type: dict[str, list[ViolationState]] = {}
            for v in self.violations:
                if v.type not in by_type:
                    by_type[v.type] = []
                by_type[v.type].append(v)

            for vtype, violations in sorted(
                by_type.items(), key=lambda x: len(x[1]), reverse=True
            ):
                lines.append(f"### {vtype}: {len(violations)}")
                for v in violations[:5]:
                    nets_str = f" [{', '.join(v.nets)}]" if v.nets else ""
                    lines.append(f"  - @({v.x:.1f}, {v.y:.1f}){nets_str}")
                if len(violations) > 5:
                    lines.append(f"  ... and {len(violations) - 5} more")
                lines.append("")

        return "\n".join(lines)
