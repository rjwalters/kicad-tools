"""
Component placement for PCB blocks.

This module provides:
- ComponentPlacement: Placement of a component within a block
- get_footprint_pads: Get pad positions for a footprint
"""

from dataclasses import dataclass, field

from kicad_tools.core.geometry import rotate_pad_offset

from .geometry import Layer, Point

# Import footprint reader for accurate pad positions
try:
    from kicad_footprint_reader import get_footprint_pads as _get_library_pads

    _FOOTPRINT_READER_AVAILABLE = True
except ImportError:
    _FOOTPRINT_READER_AVAILABLE = False
    _get_library_pads = None


def kicad_rotate(point: Point, rotation: float, origin: Point | None = None) -> Point:
    """Map a local point into its parent frame the way KiCad does.

    Rotates ``point`` by a KiCad orientation of ``rotation`` degrees and then
    translates it by ``origin``. KiCad applies orientations as a **negated**
    angle relative to the standard counter-clockwise math that
    :meth:`Point.rotate` uses (verified against pcbnew, #3739), so this
    delegates to the canonical :func:`kicad_tools.core.geometry.rotate_pad_offset`.

    It is the one transform for every block-relative -> board point (pads,
    component origins, ports, trace ends, vias; #7046), and the same one used
    for a footprint's pad offsets. Using it for both is what keeps a block
    rotated as a rigid body in KiCad's own convention, so block traces end on
    the pads KiCad draws at 90/270 degrees as well as 0/180.
    """
    rx, ry = rotate_pad_offset(point.x, point.y, rotation)
    if origin is None:
        origin = Point(0, 0)
    return Point(rx + origin.x, ry + origin.y)


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
        self, pad_name: str, block_origin: Point | None = None, block_rotation: float = 0
    ) -> Point:
        """Get absolute pad position after block placement."""
        if pad_name not in self.pads:
            raise KeyError(f"Pad '{pad_name}' not found on {self.ref}")

        # Start with pad position relative to component
        p = self.pads[pad_name]

        # Rotate by component rotation and translate to the component
        # position (relative to block), in KiCad's convention (#3739).
        p = kicad_rotate(p, self.rotation, self.position)

        # Apply block rotation, in the same convention (#7046).
        if block_rotation != 0:
            p = kicad_rotate(p, block_rotation)

        # Apply block origin
        if block_origin:
            p = p + block_origin

        return p


# Standard pad positions for common footprints (relative to footprint center)
# These would normally come from KiCad footprint files
FOOTPRINT_PADS: dict[str, dict[str, tuple[float, float]]] = {
    # 0603 capacitor/resistor (2 pads, 1.6mm apart)
    "Capacitor_SMD:C_0603_1608Metric": {
        "1": (-0.775, 0),
        "2": (0.775, 0),
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
    # SOT-23 (3-pin, e.g., transistor) -- KiCad 10 Package_TO_SOT_SMD.pretty:
    # pins 1/2 down the left side, pin 3 on the right (Issue #7022 review;
    # the old table was this one rotated 90 degrees).
    "Package_TO_SOT_SMD:SOT-23": {
        "1": (-0.9375, -0.95),
        "2": (-0.9375, 0.95),
        "3": (0.9375, 0),
    },
    # SOT-23-5 (5-pin, e.g., LDO) -- KiCad 10 Package_TO_SOT_SMD.pretty:
    # pins 1-3 down the left side, 4 bottom-right, 5 top-right.
    "Package_TO_SOT_SMD:SOT-23-5": {
        "1": (-1.1375, -0.95),
        "2": (-1.1375, 0),
        "3": (-1.1375, 0.95),
        "4": (1.1375, 0.95),
        "5": (1.1375, -0.95),
    },
    # TSSOP-20 (STM32C011) -- KiCad 10 Package_SO.pretty: pin 1 top-left, pins 1-10
    # down the left side, 11-20 up the right side.
    "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm": {
        "1": (-2.8625, -2.925),
        "2": (-2.8625, -2.275),
        "3": (-2.8625, -1.625),
        "4": (-2.8625, -0.975),
        "5": (-2.8625, -0.325),
        "6": (-2.8625, 0.325),
        "7": (-2.8625, 0.975),
        "8": (-2.8625, 1.625),
        "9": (-2.8625, 2.275),
        "10": (-2.8625, 2.925),
        "11": (2.8625, 2.925),
        "12": (2.8625, 2.275),
        "13": (2.8625, 1.625),
        "14": (2.8625, 0.975),
        "15": (2.8625, 0.325),
        "16": (2.8625, -0.325),
        "17": (2.8625, -0.975),
        "18": (2.8625, -1.625),
        "19": (2.8625, -2.275),
        "20": (2.8625, -2.925),
    },
    # 4-pin SMD oscillator -- KiCad 10 Oscillator.pretty (1 EN, 2 GND, 3 OUT, 4 VDD)
    "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm": {
        "1": (-1.05, 0.825),
        "2": (1.05, 0.825),
        "3": (1.05, -0.825),
        "4": (-1.05, -0.825),
    },
}


def get_footprint_pads(footprint: str) -> dict[str, tuple[float, float]]:
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


__all__ = ["ComponentPlacement", "get_footprint_pads", "kicad_rotate"]
