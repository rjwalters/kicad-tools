#!/usr/bin/env python3
"""
KiCad PCB Blocks - Virtual Components for Hierarchical PCB Layout

This module extends the circuit block concept to PCB layout, treating
groups of components as "virtual components" with:
- Internal component placement (relative positions)
- Internal routing (pre-routed critical traces)
- External ports (connection points on the block boundary)

This enables a "divide and conquer" approach to PCB layout:
1. Define blocks with internal placement + routing
2. Place blocks on PCB
3. Route inter-block connections (simpler problem)

Usage:
    from kicad_pcb_blocks import PCBBlock, MCUBlock, LDOBlock

    # Create MCU block with bypass caps pre-placed and pre-routed
    mcu = MCUBlock(
        mcu_footprint="QFP-20_4x4mm",
        bypass_caps=["C12", "C13"],
    )

    # Place on PCB at position
    mcu.place(x=100, y=50, rotation=0)

    # Get port positions for inter-block routing
    vdd_port = mcu.port("VDD")
    pa0_port = mcu.port("PA0")
"""

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

# Import footprint reader for accurate pad positions
try:
    from kicad_footprint_reader import get_footprint_pads as _get_library_pads

    _FOOTPRINT_READER_AVAILABLE = True
except ImportError:
    _FOOTPRINT_READER_AVAILABLE = False
    _get_library_pads = None


# =============================================================================
# Geometry Primitives
# =============================================================================


@dataclass
class Point:
    """2D point in mm."""

    x: float
    y: float

    def __add__(self, other: "Point") -> "Point":
        return Point(self.x + other.x, self.y + other.y)

    def __sub__(self, other: "Point") -> "Point":
        return Point(self.x - other.x, self.y - other.y)

    def rotate(self, angle_deg: float, origin: "Point" = None) -> "Point":
        """Rotate point around origin (default: 0,0)."""
        if origin is None:
            origin = Point(0, 0)

        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)

        # Translate to origin
        dx = self.x - origin.x
        dy = self.y - origin.y

        # Rotate
        new_x = dx * cos_a - dy * sin_a
        new_y = dx * sin_a + dy * cos_a

        # Translate back
        return Point(new_x + origin.x, new_y + origin.y)

    def tuple(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Rectangle:
    """Axis-aligned bounding box."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    @property
    def center(self) -> Point:
        return Point((self.min_x + self.max_x) / 2, (self.min_y + self.max_y) / 2)

    def contains(self, p: Point) -> bool:
        return self.min_x <= p.x <= self.max_x and self.min_y <= p.y <= self.max_y

    def expand(self, margin: float) -> "Rectangle":
        """Return expanded rectangle."""
        return Rectangle(
            self.min_x - margin, self.min_y - margin, self.max_x + margin, self.max_y + margin
        )


class Layer(Enum):
    """PCB layers."""

    F_CU = "F.Cu"  # Front copper
    B_CU = "B.Cu"  # Back copper
    F_SILK = "F.SilkS"  # Front silkscreen
    B_SILK = "B.SilkS"  # Back silkscreen
    F_MASK = "F.Mask"  # Front solder mask
    B_MASK = "B.Mask"  # Back solder mask
    F_PASTE = "F.Paste"  # Front solder paste
    B_PASTE = "B.Paste"  # Back solder paste
    EDGE = "Edge.Cuts"  # Board outline


# =============================================================================
# PCB Elements
# =============================================================================


@dataclass
class Pad:
    """A pad/connection point."""

    name: str
    position: Point
    layer: Layer = Layer.F_CU
    net: Optional[str] = None

    # Pad geometry (for actual pads, not just ports)
    shape: str = "circle"  # circle, rect, oval
    size: tuple[float, float] = (0.8, 0.8)  # mm
    drill: Optional[float] = None  # For through-hole


@dataclass
class Port:
    """
    External connection point on a block's boundary.

    A port is a virtual pad that represents where external traces
    should connect to this block. The actual physical connection
    might be to a component pad inside the block.
    """

    name: str
    position: Point  # Position relative to block origin
    layer: Layer = Layer.F_CU
    direction: str = "inout"  # in, out, inout, power
    net: Optional[str] = None  # Net name when connected

    # What this port connects to inside the block
    internal_pad: Optional[str] = None  # e.g., "U1.VDD" or "C12.1"


@dataclass
class TraceSegment:
    """A segment of copper trace."""

    start: Point
    end: Point
    width: float = 0.25  # mm
    layer: Layer = Layer.F_CU
    net: Optional[str] = None


@dataclass
class Via:
    """A via connecting layers."""

    position: Point
    drill: float = 0.3  # mm
    size: float = 0.6  # mm (annular ring outer diameter)
    layers: tuple[Layer, Layer] = (Layer.F_CU, Layer.B_CU)
    net: Optional[str] = None


@dataclass
class ComponentPlacement:
    """Placement of a component within a block."""

    ref: str  # Reference designator (U1, C12, etc.)
    footprint: str  # KiCad footprint name
    position: Point  # Position relative to block origin
    rotation: float = 0  # Degrees
    layer: Layer = Layer.F_CU  # F.Cu = top, B.Cu = bottom

    # Pad positions (relative to component position, before rotation)
    pads: dict[str, Point] = field(default_factory=dict)

    def pad_position(
        self, pad_name: str, block_origin: Point = None, block_rotation: float = 0
    ) -> Point:
        """Get absolute pad position after block placement."""
        if pad_name not in self.pads:
            raise KeyError(f"Pad '{pad_name}' not found on {self.ref}")

        # Start with pad position relative to component
        p = self.pads[pad_name]

        # Rotate by component rotation
        p = p.rotate(self.rotation)

        # Translate to component position (relative to block)
        p = p + self.position

        # Apply block rotation
        if block_rotation != 0:
            p = p.rotate(block_rotation)

        # Apply block origin
        if block_origin:
            p = p + block_origin

        return p


# =============================================================================
# PCB Block Base Class
# =============================================================================


class PCBBlock:
    """
    A virtual component - a group of components with internal routing.

    Think of this as a "macro component" that encapsulates:
    - Multiple physical components (IC + bypass caps, etc.)
    - Internal trace routing (critical short traces)
    - External ports (where to connect from outside)

    The block can be placed and rotated as a unit, and external
    routing only needs to connect to the ports.
    """

    def __init__(self, name: str = "block"):
        self.name = name

        # Block placement (set by place())
        self.origin: Point = Point(0, 0)
        self.rotation: float = 0
        self.placed: bool = False

        # Internal elements (positions relative to block origin)
        self.components: dict[str, ComponentPlacement] = {}
        self.traces: list[TraceSegment] = []
        self.vias: list[Via] = []

        # External interface
        self.ports: dict[str, Port] = {}

        # Computed after components added
        self._bounding_box: Optional[Rectangle] = None

    def add_component(
        self,
        ref: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float = 0,
        pads: dict[str, tuple] = None,
        layer: Layer = Layer.F_CU,
    ) -> ComponentPlacement:
        """Add a component to the block."""
        pad_points = {}
        if pads:
            pad_points = {name: Point(p[0], p[1]) for name, p in pads.items()}

        comp = ComponentPlacement(
            ref=ref,
            footprint=footprint,
            position=Point(x, y),
            rotation=rotation,
            layer=layer,
            pads=pad_points,
        )
        self.components[ref] = comp
        self._bounding_box = None  # Invalidate cache
        return comp

    def add_trace(
        self,
        start: tuple | Point,
        end: tuple | Point,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str = None,
    ) -> TraceSegment:
        """Add an internal trace segment."""
        if isinstance(start, tuple):
            start = Point(start[0], start[1])
        if isinstance(end, tuple):
            end = Point(end[0], end[1])

        trace = TraceSegment(start=start, end=end, width=width, layer=layer, net=net)
        self.traces.append(trace)
        return trace

    def add_via(
        self, x: float, y: float, net: str = None, drill: float = 0.3, size: float = 0.6
    ) -> Via:
        """Add an internal via."""
        via = Via(position=Point(x, y), drill=drill, size=size, net=net)
        self.vias.append(via)
        return via

    def add_port(
        self,
        name: str,
        x: float,
        y: float,
        direction: str = "inout",
        internal_pad: str = None,
        layer: Layer = Layer.F_CU,
    ) -> Port:
        """
        Add an external port to the block.

        Args:
            name: Port name (e.g., "VDD", "PA0")
            x, y: Position relative to block origin
            direction: "in", "out", "inout", or "power"
            internal_pad: What this connects to inside (e.g., "U1.VDD")
        """
        port = Port(
            name=name,
            position=Point(x, y),
            layer=layer,
            direction=direction,
            internal_pad=internal_pad,
        )
        self.ports[name] = port
        return port

    def route_to_port(
        self,
        pad_ref: str,
        port_name: str,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str = None,
    ):
        """
        Add trace from internal pad to external port.

        Args:
            pad_ref: "REF.PAD" format (e.g., "U1.VDD", "C12.1")
            port_name: Name of port to route to
        """
        # Parse pad reference
        ref, pad_name = pad_ref.split(".")
        if ref not in self.components:
            raise KeyError(f"Component '{ref}' not found in block")

        comp = self.components[ref]
        pad_pos = comp.pad_position(pad_name)

        if port_name not in self.ports:
            raise KeyError(f"Port '{port_name}' not found in block")

        port_pos = self.ports[port_name].position

        self.add_trace(pad_pos, port_pos, width=width, layer=layer, net=net)

    def place(self, x: float, y: float, rotation: float = 0):
        """Place the block on the PCB."""
        self.origin = Point(x, y)
        self.rotation = rotation
        self.placed = True

    def port(self, name: str) -> Point:
        """Get absolute position of a port after placement."""
        if name not in self.ports:
            available = list(self.ports.keys())
            raise KeyError(f"Port '{name}' not found. Available: {available}")

        # Get port position relative to block
        rel_pos = self.ports[name].position

        # Apply block rotation
        if self.rotation != 0:
            rel_pos = rel_pos.rotate(self.rotation)

        # Apply block origin
        return rel_pos + self.origin

    def component_position(self, ref: str) -> Point:
        """Get absolute position of a component after placement."""
        if ref not in self.components:
            raise KeyError(f"Component '{ref}' not found in block")

        comp = self.components[ref]
        rel_pos = comp.position

        # Apply block rotation
        if self.rotation != 0:
            rel_pos = rel_pos.rotate(self.rotation)

        return rel_pos + self.origin

    @property
    def bounding_box(self) -> Rectangle:
        """Get bounding box of all components."""
        if self._bounding_box is not None:
            return self._bounding_box

        if not self.components:
            return Rectangle(0, 0, 0, 0)

        # Simple bbox from component positions
        # TODO: Include actual footprint sizes
        xs = [c.position.x for c in self.components.values()]
        ys = [c.position.y for c in self.components.values()]

        self._bounding_box = Rectangle(
            min(xs) - 2,
            min(ys) - 2,  # 2mm margin
            max(xs) + 2,
            max(ys) + 2,
        )
        return self._bounding_box

    def get_placed_components(self) -> list[dict]:
        """Get components with absolute positions for PCB export."""
        result = []
        for ref, comp in self.components.items():
            pos = self.component_position(ref)
            result.append(
                {
                    "ref": ref,
                    "footprint": comp.footprint,
                    "x": pos.x,
                    "y": pos.y,
                    "rotation": (comp.rotation + self.rotation) % 360,
                    "layer": comp.layer.value,
                }
            )
        return result

    def get_placed_traces(self) -> list[dict]:
        """Get traces with absolute positions for PCB export."""
        result = []
        for trace in self.traces:
            start = trace.start
            end = trace.end

            # Apply block transformation
            if self.rotation != 0:
                start = start.rotate(self.rotation)
                end = end.rotate(self.rotation)

            start = start + self.origin
            end = end + self.origin

            result.append(
                {
                    "start": start.tuple(),
                    "end": end.tuple(),
                    "width": trace.width,
                    "layer": trace.layer.value,
                    "net": trace.net,
                }
            )
        return result

    def __repr__(self):
        placed = f" at ({self.origin.x}, {self.origin.y})" if self.placed else ""
        return f"PCBBlock({self.name}, {len(self.components)} components, {len(self.ports)} ports{placed})"


# =============================================================================
# Common Footprint Data
# =============================================================================

# Standard pad positions for common footprints (relative to footprint center)
# These would normally come from KiCad footprint files

FOOTPRINT_PADS = {
    # 0603 capacitor/resistor (2 pads, 1.6mm apart)
    "Capacitor_SMD:C_0603_1608Metric": {
        "1": (-0.8, 0),
        "2": (0.8, 0),
    },
    "Resistor_SMD:R_0603_1608Metric": {
        "1": (-0.8, 0),
        "2": (0.8, 0),
    },
    # 0805 capacitor/resistor (2 pads, 2.0mm apart)
    "Capacitor_SMD:C_0805_2012Metric": {
        "1": (-1.0, 0),
        "2": (1.0, 0),
    },
    # SOT-23 (3-pin, e.g., transistor)
    "Package_TO_SOT_SMD:SOT-23": {
        "1": (-0.95, 1.1),
        "2": (0.95, 1.1),
        "3": (0, -1.1),
    },
    # SOT-23-5 (5-pin, e.g., LDO)
    "Package_TO_SOT_SMD:SOT-23-5": {
        "1": (-0.95, 1.1),
        "2": (0, 1.1),
        "3": (0.95, 1.1),
        "4": (0.95, -1.1),
        "5": (-0.95, -1.1),
    },
    # TSSOP-20 (STM32C011)
    "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm": {
        # Left side (pins 1-10, bottom to top)
        "1": (-2.95, 2.925),
        "2": (-2.95, 2.275),
        "3": (-2.95, 1.625),
        "4": (-2.95, 0.975),
        "5": (-2.95, 0.325),
        "6": (-2.95, -0.325),
        "7": (-2.95, -0.975),
        "8": (-2.95, -1.625),
        "9": (-2.95, -2.275),
        "10": (-2.95, -2.925),
        # Right side (pins 11-20, bottom to top)
        "11": (2.95, -2.925),
        "12": (2.95, -2.275),
        "13": (2.95, -1.625),
        "14": (2.95, -0.975),
        "15": (2.95, -0.325),
        "16": (2.95, 0.325),
        "17": (2.95, 0.975),
        "18": (2.95, 1.625),
        "19": (2.95, 2.275),
        "20": (2.95, 2.925),
    },
}


def get_footprint_pads(footprint: str) -> dict[str, tuple]:
    """Get pad positions for a footprint.

    Uses the footprint reader library if available, otherwise falls back
    to built-in data.
    """
    # Try the footprint reader library first (has accurate data)
    if _FOOTPRINT_READER_AVAILABLE and _get_library_pads is not None:
        return _get_library_pads(footprint)

    # Fall back to built-in data
    if footprint in FOOTPRINT_PADS:
        return FOOTPRINT_PADS[footprint]

    # Default: assume 2-pad component
    return {"1": (-0.8, 0), "2": (0.8, 0)}


# =============================================================================
# Pre-defined Block Types
# =============================================================================


class MCUBlock(PCBBlock):
    """
    MCU with bypass capacitors.

    Places the MCU with bypass caps positioned optimally close to
    VDD/VSS pins, with internal routing for power connections.

    Example for STM32C011 (TSSOP-20):
        - Pin 4 = VDD (left side)
        - Pin 5 = VSS (left side)
        - Bypass caps placed to left of chip
    """

    def __init__(
        self,
        mcu_ref: str = "U1",
        mcu_footprint: str = "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
        bypass_caps: list[str] = None,
        cap_footprint: str = "Capacitor_SMD:C_0603_1608Metric",
        vdd_pin: str = "4",
        vss_pin: str = "5",
    ):
        super().__init__(name=f"MCU_{mcu_ref}")

        if bypass_caps is None:
            bypass_caps = ["C1", "C2"]

        # Get footprint pad data
        mcu_pads = get_footprint_pads(mcu_footprint)
        cap_pads = get_footprint_pads(cap_footprint)

        # Place MCU at block center
        self.mcu = self.add_component(mcu_ref, mcu_footprint, 0, 0, pads=mcu_pads)

        # VDD/VSS pad positions
        vdd_pos = Point(mcu_pads[vdd_pin][0], mcu_pads[vdd_pin][1])
        vss_pos = Point(mcu_pads[vss_pin][0], mcu_pads[vss_pin][1])

        # Place bypass caps close to power pins
        # Caps oriented horizontally, positioned to the left of VDD/VSS
        cap_x = vdd_pos.x - 2.5  # 2.5mm left of MCU edge
        cap_spacing = 2.0

        for i, cap_ref in enumerate(bypass_caps):
            cap_y = (vdd_pos.y + vss_pos.y) / 2 + (i - len(bypass_caps) / 2 + 0.5) * cap_spacing
            self.add_component(
                cap_ref,
                cap_footprint,
                cap_x,
                cap_y,
                rotation=90,  # Rotate for vertical orientation
                pads=cap_pads,
            )

        # Internal routing: VDD to cap pin 1, cap pin 2 to VSS
        trace_width = 0.3  # Power traces wider

        for cap_ref in bypass_caps:
            cap = self.components[cap_ref]
            cap_pad1 = cap.pad_position("1")
            cap_pad2 = cap.pad_position("2")

            # VDD trace: MCU VDD → cap pin 1
            self.add_trace(vdd_pos, cap_pad1, width=trace_width, net="VDD")

            # VSS trace: cap pin 2 → MCU VSS
            self.add_trace(cap_pad2, vss_pos, width=trace_width, net="GND")

        # External ports - positioned at block edges
        # Power ports on left side
        self.add_port(
            "VDD", cap_x - 2, vdd_pos.y, direction="power", internal_pad=f"{bypass_caps[0]}.1"
        )
        self.add_port(
            "GND", cap_x - 2, vss_pos.y, direction="power", internal_pad=f"{bypass_caps[-1]}.2"
        )

        # Signal ports on right side (expose MCU pins)
        # This would be customized based on actual pin usage
        right_edge = 5.0  # Right edge of block

        # Example: expose PA0-PA7 as ports
        for pin_num in range(7, 15):  # Pins 7-14 are on right side for TSSOP-20
            pin_name = str(pin_num)
            if pin_name in mcu_pads:
                pin_pos = Point(mcu_pads[pin_name][0], mcu_pads[pin_name][1])
                self.add_port(
                    f"PIN{pin_num}",
                    right_edge,
                    pin_pos.y,
                    direction="inout",
                    internal_pad=f"{mcu_ref}.{pin_num}",
                )


class LDOBlock(PCBBlock):
    """
    LDO regulator with input and output capacitors.

    Standard layout:
        C_in ── LDO ── C_out1 ── C_out2
               │
              GND
    """

    def __init__(
        self,
        ldo_ref: str = "U1",
        ldo_footprint: str = "Package_TO_SOT_SMD:SOT-23-5",
        input_cap: str = "C1",
        output_caps: list[str] = None,
        cap_footprint: str = "Capacitor_SMD:C_0805_2012Metric",
    ):
        super().__init__(name=f"LDO_{ldo_ref}")

        if output_caps is None:
            output_caps = ["C2", "C3"]

        ldo_pads = get_footprint_pads(ldo_footprint)
        cap_pads = get_footprint_pads(cap_footprint)

        # SOT-23-5 pinout (typical LDO like AP2204):
        # Pin 1: VIN (top left)
        # Pin 2: GND (top center)
        # Pin 3: EN (top right)
        # Pin 4: NC or BYPASS (bottom right)
        # Pin 5: VOUT (bottom left)

        # Place LDO at center
        self.ldo = self.add_component(ldo_ref, ldo_footprint, 0, 0, pads=ldo_pads)

        # Place input cap to the left
        self.add_component(input_cap, cap_footprint, -3.5, 0, rotation=90, pads=cap_pads)

        # Place output caps to the right
        for i, cap_ref in enumerate(output_caps):
            self.add_component(cap_ref, cap_footprint, 3.5 + i * 2.5, 0, rotation=90, pads=cap_pads)

        # Internal routing
        trace_width = 0.4  # Power traces

        # VIN connections
        vin_pos = Point(ldo_pads["1"][0], ldo_pads["1"][1])
        cin = self.components[input_cap]
        cin_pad1 = cin.pad_position("1")
        self.add_trace(vin_pos, cin_pad1, width=trace_width, net="VIN")

        # VOUT connections
        vout_pos = Point(ldo_pads["5"][0], ldo_pads["5"][1])
        for cap_ref in output_caps:
            cout = self.components[cap_ref]
            cout_pad1 = cout.pad_position("1")
            self.add_trace(vout_pos, cout_pad1, width=trace_width, net="VOUT")

        # GND connections
        gnd_pos = Point(ldo_pads["2"][0], ldo_pads["2"][1])
        cin_pad2 = cin.pad_position("2")
        self.add_trace(gnd_pos, cin_pad2, width=trace_width, net="GND")
        for cap_ref in output_caps:
            cout = self.components[cap_ref]
            cout_pad2 = cout.pad_position("2")
            self.add_trace(gnd_pos, cout_pad2, width=trace_width, net="GND")

        # External ports
        left_edge = -5.5
        right_edge = 3.5 + len(output_caps) * 2.5 + 1.5

        self.add_port("VIN", left_edge, 0, direction="power")
        self.add_port("VOUT", right_edge, 0, direction="power")
        self.add_port("GND", 0, 3, direction="power")
        self.add_port("EN", 2, -3, direction="in", internal_pad=f"{ldo_ref}.3")


class OscillatorBlock(PCBBlock):
    """
    Crystal oscillator with decoupling capacitor.
    """

    def __init__(
        self,
        osc_ref: str = "Y1",
        osc_footprint: str = "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
        cap_ref: str = "C1",
        cap_footprint: str = "Capacitor_SMD:C_0603_1608Metric",
    ):
        super().__init__(name=f"OSC_{osc_ref}")

        # Simplified oscillator pads (4-pin)
        osc_pads = {
            "1": (-1.25, -0.95),  # EN
            "2": (-1.25, 0.95),  # GND
            "3": (1.25, 0.95),  # OUT
            "4": (1.25, -0.95),  # VDD
        }
        cap_pads = get_footprint_pads(cap_footprint)

        # Place oscillator
        self.add_component(osc_ref, osc_footprint, 0, 0, pads=osc_pads)

        # Place decoupling cap near VDD
        self.add_component(cap_ref, cap_footprint, 3.0, -1, rotation=0, pads=cap_pads)

        # Internal routing: VDD to cap
        vdd_pos = Point(osc_pads["4"][0], osc_pads["4"][1])
        cap = self.components[cap_ref]
        self.add_trace(vdd_pos, cap.pad_position("1"), width=0.3, net="VDD")

        gnd_pos = Point(osc_pads["2"][0], osc_pads["2"][1])
        self.add_trace(gnd_pos, cap.pad_position("2"), width=0.3, net="GND")

        # External ports
        self.add_port("VDD", 5, -1, direction="power")
        self.add_port("GND", -3, 1, direction="power")
        self.add_port("OUT", 3, 1, direction="out", internal_pad=f"{osc_ref}.3")
        self.add_port("EN", -3, -1, direction="in", internal_pad=f"{osc_ref}.1")


class LEDBlock(PCBBlock):
    """
    LED with current-limiting resistor.
    """

    def __init__(
        self,
        led_ref: str = "D1",
        res_ref: str = "R1",
        led_footprint: str = "LED_SMD:LED_0603_1608Metric",
        res_footprint: str = "Resistor_SMD:R_0603_1608Metric",
    ):
        super().__init__(name=f"LED_{led_ref}")

        led_pads = {"1": (-0.8, 0), "2": (0.8, 0)}  # 1=cathode, 2=anode
        res_pads = get_footprint_pads(res_footprint)

        # LED and resistor in line
        self.add_component(led_ref, led_footprint, 0, 0, pads=led_pads)
        self.add_component(res_ref, res_footprint, 3.0, 0, pads=res_pads)

        # LED cathode to resistor
        led = self.components[led_ref]
        res = self.components[res_ref]
        self.add_trace(led.pad_position("1"), res.pad_position("1"), width=0.25, net="LED_MID")

        # External ports
        self.add_port("ANODE", -2.5, 0, direction="in", internal_pad=f"{led_ref}.2")
        self.add_port("CATHODE", 5.5, 0, direction="out", internal_pad=f"{res_ref}.2")


# =============================================================================
# Block Placement and Export
# =============================================================================


class PCBLayout:
    """
    Container for placing and routing multiple PCB blocks.
    """

    def __init__(self, name: str = "layout"):
        self.name = name
        self.blocks: dict[str, PCBBlock] = {}
        self.inter_block_traces: list[TraceSegment] = []
        self.inter_block_vias: list[Via] = []

    def add_block(self, block: PCBBlock, name: str = None) -> PCBBlock:
        """Add a block to the layout."""
        if name is None:
            name = block.name
        self.blocks[name] = block
        return block

    def route(
        self,
        from_block: str,
        from_port: str,
        to_block: str,
        to_port: str,
        width: float = 0.25,
        layer: Layer = Layer.F_CU,
        net: str = None,
    ):
        """
        Route between two block ports.

        This creates a simple direct trace. More complex routing
        would use waypoints or an autorouter.
        """
        start = self.blocks[from_block].port(from_port)
        end = self.blocks[to_block].port(to_port)

        trace = TraceSegment(start=start, end=end, width=width, layer=layer, net=net)
        self.inter_block_traces.append(trace)
        return trace

    def export_placements(self) -> list[dict]:
        """Export all component placements."""
        result = []
        for block in self.blocks.values():
            result.extend(block.get_placed_components())
        return result

    def export_traces(self) -> list[dict]:
        """Export all traces (internal + inter-block)."""
        result = []

        # Internal traces from each block
        for block in self.blocks.values():
            result.extend(block.get_placed_traces())

        # Inter-block traces
        for trace in self.inter_block_traces:
            result.append(
                {
                    "start": trace.start.tuple(),
                    "end": trace.end.tuple(),
                    "width": trace.width,
                    "layer": trace.layer.value,
                    "net": trace.net,
                }
            )

        return result

    def summary(self) -> str:
        """Print layout summary."""
        lines = [f"PCB Layout: {self.name}", "=" * 40]

        total_components = 0
        total_internal_traces = 0

        for name, block in self.blocks.items():
            n_comp = len(block.components)
            n_traces = len(block.traces)
            _n_ports = len(block.ports)  # noqa: F841 - used in port display below
            total_components += n_comp
            total_internal_traces += n_traces

            pos = f"({block.origin.x}, {block.origin.y})" if block.placed else "not placed"
            lines.append(f"\n{name}: {pos}")
            lines.append(f"  Components: {n_comp}")
            lines.append(f"  Internal traces: {n_traces}")
            lines.append(f"  Ports: {', '.join(block.ports.keys())}")

        lines.append(f"\n{'=' * 40}")
        lines.append(f"Total components: {total_components}")
        lines.append(f"Total internal traces: {total_internal_traces}")
        lines.append(f"Inter-block traces: {len(self.inter_block_traces)}")

        return "\n".join(lines)


# =============================================================================
# KiCad PCB Export
# =============================================================================


class KiCadPCBExporter:
    """
    Export PCBLayout to KiCad PCB format (.kicad_pcb).

    Can either:
    1. Generate a new PCB file with placements and traces
    2. Update an existing PCB file with new placements/traces

    Usage:
        exporter = KiCadPCBExporter(layout)
        exporter.write("output.kicad_pcb")

        # Or update existing file
        exporter.update_placements("existing.kicad_pcb", "output.kicad_pcb")
    """

    def __init__(self, layout: PCBLayout):
        self.layout = layout
        self.nets: dict[str, int] = {}  # net name -> net number
        self._next_net = 1

    def _get_net_number(self, net_name: str) -> int:
        """Get or assign a net number."""
        if net_name is None:
            return 0
        if net_name not in self.nets:
            self.nets[net_name] = self._next_net
            self._next_net += 1
        return self.nets[net_name]

    def _uuid(self) -> str:
        """Generate a UUID for KiCad elements."""
        import uuid

        return str(uuid.uuid4())

    def _format_coord(self, val: float) -> str:
        """Format coordinate value."""
        return f"{val:.4f}"

    def _generate_header(self, title: str = "PCB Layout") -> str:
        """Generate PCB file header."""
        return f'''(kicad_pcb
	(version 20241229)
	(generator "kicad_pcb_blocks.py")
	(generator_version "1.0")
	(general
		(thickness 1.6)
		(legacy_teardrops no)
	)
	(paper "A4")
	(title_block
		(title "{title}")
		(date "2025-01")
		(rev "A")
		(comment 1 "Generated by kicad_pcb_blocks.py")
		(comment 2 "Virtual component / block-based layout")
	)
	(layers
		(0 "F.Cu" signal)
		(31 "B.Cu" signal)
		(32 "B.Adhes" user "B.Adhesive")
		(33 "F.Adhes" user "F.Adhesive")
		(34 "B.Paste" user)
		(35 "F.Paste" user)
		(36 "B.SilkS" user "B.Silkscreen")
		(37 "F.SilkS" user "F.Silkscreen")
		(38 "B.Mask" user)
		(39 "F.Mask" user)
		(40 "Dwgs.User" user "User.Drawings")
		(41 "Cmts.User" user "User.Comments")
		(44 "Edge.Cuts" user)
		(46 "B.CrtYd" user "B.Courtyard")
		(47 "F.CrtYd" user "F.Courtyard")
		(48 "B.Fab" user)
		(49 "F.Fab" user)
	)
	(setup
		(pad_to_mask_clearance 0.05)
		(allow_soldermask_bridges_in_footprints no)
	)
'''

    def _generate_nets(self) -> str:
        """Generate net definitions."""
        lines = ['\t(net 0 "")']

        # Collect all nets from traces
        for block in self.layout.blocks.values():
            for trace in block.traces:
                if trace.net:
                    self._get_net_number(trace.net)

        for trace in self.layout.inter_block_traces:
            if trace.net:
                self._get_net_number(trace.net)

        # Generate net lines
        for net_name, net_num in sorted(self.nets.items(), key=lambda x: x[1]):
            lines.append(f'\t(net {net_num} "{net_name}")')

        return "\n".join(lines)

    def _generate_footprint(
        self,
        ref: str,
        footprint: str,
        x: float,
        y: float,
        rotation: float,
        layer: str,
        value: str = "",
    ) -> str:
        """Generate a footprint placement."""
        uuid = self._uuid()
        ref_uuid = self._uuid()
        val_uuid = self._uuid()
        fp_uuid = self._uuid()

        return f'''	(footprint "{footprint}"
		(layer "{layer}")
		(uuid "{uuid}")
		(at {self._format_coord(x)} {self._format_coord(y)} {rotation})
		(property "Reference" "{ref}"
			(at 0 -2 0)
			(layer "F.SilkS")
			(uuid "{ref_uuid}")
			(effects
				(font
					(size 0.8 0.8)
					(thickness 0.12)
				)
			)
		)
		(property "Value" "{value}"
			(at 0 2 0)
			(layer "F.Fab")
			(uuid "{val_uuid}")
			(effects
				(font
					(size 0.8 0.8)
					(thickness 0.12)
				)
			)
		)
		(property "Footprint" "{footprint}"
			(at 0 0 0)
			(layer "F.Fab")
			(hide yes)
			(uuid "{fp_uuid}")
			(effects
				(font
					(size 1 1)
					(thickness 0.15)
				)
			)
		)
	)'''

    def _generate_segment(
        self, start: tuple, end: tuple, width: float, layer: str, net: int
    ) -> str:
        """Generate a trace segment."""
        uuid = self._uuid()
        return f'''	(segment
		(start {self._format_coord(start[0])} {self._format_coord(start[1])})
		(end {self._format_coord(end[0])} {self._format_coord(end[1])})
		(width {width})
		(layer "{layer}")
		(net {net})
		(uuid "{uuid}")
	)'''

    def _generate_via(
        self, x: float, y: float, size: float, drill: float, layers: tuple, net: int
    ) -> str:
        """Generate a via."""
        uuid = self._uuid()
        layer_str = f'"{layers[0]}" "{layers[1]}"'
        return f'''	(via
		(at {self._format_coord(x)} {self._format_coord(y)})
		(size {size})
		(drill {drill})
		(layers {layer_str})
		(net {net})
		(uuid "{uuid}")
	)'''

    def generate(self, title: str = None) -> str:
        """Generate complete KiCad PCB file content."""
        if title is None:
            title = self.layout.name

        sections = []

        # Header
        sections.append(self._generate_header(title))

        # Nets
        sections.append(self._generate_nets())

        # Footprints
        footprint_lines = []
        for placement in self.layout.export_placements():
            fp = self._generate_footprint(
                ref=placement["ref"],
                footprint=placement["footprint"],
                x=placement["x"],
                y=placement["y"],
                rotation=placement["rotation"],
                layer=placement["layer"],
                value="",  # Could add component values
            )
            footprint_lines.append(fp)
        sections.append("\n".join(footprint_lines))

        # Traces (segments)
        trace_lines = []
        for trace in self.layout.export_traces():
            net_num = self._get_net_number(trace["net"])
            seg = self._generate_segment(
                start=trace["start"],
                end=trace["end"],
                width=trace["width"],
                layer=trace["layer"],
                net=net_num,
            )
            trace_lines.append(seg)
        sections.append("\n".join(trace_lines))

        # Vias
        via_lines = []
        for block in self.layout.blocks.values():
            for via in block.vias:
                # Transform via position
                pos = via.position
                if block.rotation != 0:
                    pos = pos.rotate(block.rotation)
                pos = pos + block.origin

                net_num = self._get_net_number(via.net)
                v = self._generate_via(
                    x=pos.x,
                    y=pos.y,
                    size=via.size,
                    drill=via.drill,
                    layers=(via.layers[0].value, via.layers[1].value),
                    net=net_num,
                )
                via_lines.append(v)

        for via in self.layout.inter_block_vias:
            net_num = self._get_net_number(via.net)
            v = self._generate_via(
                x=via.position.x,
                y=via.position.y,
                size=via.size,
                drill=via.drill,
                layers=(via.layers[0].value, via.layers[1].value),
                net=net_num,
            )
            via_lines.append(v)

        if via_lines:
            sections.append("\n".join(via_lines))

        # Close
        sections.append(")")

        return "\n".join(sections)

    def write(self, filepath: str):
        """Write PCB to file."""
        from pathlib import Path

        content = self.generate()
        Path(filepath).write_text(content)
        print(f"Wrote PCB: {filepath}")

    def generate_placement_update(self) -> dict[str, tuple]:
        """
        Generate component positions for updating an existing PCB.

        Returns dict of {ref: (x, y, rotation)} for each component.
        """
        result = {}
        for placement in self.layout.export_placements():
            result[placement["ref"]] = (placement["x"], placement["y"], placement["rotation"])
        return result

    def generate_trace_segments(self) -> list[str]:
        """Generate trace segment S-expressions that can be appended to existing PCB."""
        segments = []
        for trace in self.layout.export_traces():
            net_num = self._get_net_number(trace["net"])
            seg = self._generate_segment(
                start=trace["start"],
                end=trace["end"],
                width=trace["width"],
                layer=trace["layer"],
                net=net_num,
            )
            segments.append(seg)
        return segments


def update_pcb_placements(source_pcb: str, layout: PCBLayout, output_pcb: str):
    """
    Update component positions in an existing PCB file based on block layout.

    This reads an existing PCB, updates the (at x y rotation) for each
    component that exists in the layout, and writes the result.

    Args:
        source_pcb: Path to existing KiCad PCB file
        layout: PCBLayout with desired component positions
        output_pcb: Path for output PCB file
    """
    import re
    from pathlib import Path

    # Get desired positions
    exporter = KiCadPCBExporter(layout)
    positions = exporter.generate_placement_update()

    # Read existing PCB
    content = Path(source_pcb).read_text()

    # For each component, find and update its position
    for ref, (x, y, rotation) in positions.items():
        # Pattern to find this component's footprint block and its position
        # Looking for: (property "Reference" "REF" ...) within a footprint
        # Then finding the (at X Y R) line above it

        # Find footprint containing this reference
        pattern = (
            rf'(\(footprint\s+"[^"]+"\s+.*?\(property\s+"Reference"\s+"{re.escape(ref)}".*?\))'
        )

        def update_position(match):
            footprint_text = match.group(1)
            # Update the (at ...) line within this footprint
            at_pattern = r"\(at\s+[\d.-]+\s+[\d.-]+(?:\s+[\d.-]+)?\)"
            new_at = f"(at {x:.4f} {y:.4f} {rotation})"
            updated = re.sub(at_pattern, new_at, footprint_text, count=1)
            return updated

        content = re.sub(pattern, update_position, content, flags=re.DOTALL)

    # Write result
    Path(output_pcb).write_text(content)
    print(f"Updated PCB placements: {output_pcb}")
    print(f"  Updated {len(positions)} components")


# =============================================================================
# Demo / Test
# =============================================================================

if __name__ == "__main__":
    print("PCB Blocks Demo")
    print("=" * 60)

    # Create layout
    layout = PCBLayout("example_board")

    # Create and place blocks
    mcu = MCUBlock(mcu_ref="U3", bypass_caps=["C12", "C13"])
    mcu.place(100, 50)
    layout.add_block(mcu, "MCU")

    ldo = LDOBlock(ldo_ref="U1", input_cap="C1", output_caps=["C2", "C3"])
    ldo.place(60, 50)
    layout.add_block(ldo, "LDO")

    osc = OscillatorBlock(osc_ref="Y1", cap_ref="C7")
    osc.place(100, 30)
    layout.add_block(osc, "OSC")

    led = LEDBlock(led_ref="D1", res_ref="R1")
    led.place(60, 30)
    layout.add_block(led, "PWR_LED")

    # Route between blocks
    layout.route("LDO", "VOUT", "MCU", "VDD", width=0.4, net="3V3")
    layout.route("LDO", "GND", "MCU", "GND", width=0.4, net="GND")
    layout.route("LDO", "VOUT", "OSC", "VDD", width=0.3, net="3V3")
    layout.route("LDO", "VOUT", "PWR_LED", "ANODE", width=0.25, net="3V3")

    # Print summary
    print(layout.summary())

    print("\n" + "=" * 60)
    print("Component Placements:")
    for p in layout.export_placements():
        print(f"  {p['ref']}: ({p['x']:.2f}, {p['y']:.2f}) @ {p['rotation']}°")

    print("\nInter-block traces:")
    for i, t in enumerate(layout.inter_block_traces):
        print(f"  {i + 1}: {t.start.tuple()} → {t.end.tuple()} ({t.net})")

    # Export to KiCad PCB
    print("\n" + "=" * 60)
    print("Generating KiCad PCB file...")

    exporter = KiCadPCBExporter(layout)
    output_path = "/tmp/example-blocks.kicad_pcb"
    exporter.write(output_path)

    print(f"\nNets defined: {list(exporter.nets.keys())}")
    print(f"\nOpen in KiCad: {output_path}")
