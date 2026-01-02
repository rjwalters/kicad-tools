"""
Component placement for PCB blocks.

This module provides:
- ComponentPlacement: Placement of a component within a block
- FOOTPRINT_PADS: Standard pad positions for common footprints
- get_footprint_pads: Get pad positions for a footprint
"""

from dataclasses import dataclass, field

from .geometry import Layer, Point

# Import footprint reader for accurate pad positions
try:
    from kicad_footprint_reader import get_footprint_pads as _get_library_pads

    _FOOTPRINT_READER_AVAILABLE = True
except ImportError:
    _FOOTPRINT_READER_AVAILABLE = False
    _get_library_pads = None


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


# Standard pad positions for common footprints (relative to footprint center)
# These would normally come from KiCad footprint files
FOOTPRINT_PADS: dict[str, dict[str, tuple[float, float]]] = {
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


__all__ = ["ComponentPlacement", "FOOTPRINT_PADS", "get_footprint_pads"]
