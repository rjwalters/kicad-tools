#!/usr/bin/env python3
"""
KiCad Footprint Reader

Reads pad positions from KiCad footprint files (.kicad_mod) or uses
built-in data for common footprints from KiCad's standard libraries.

Usage::

    from kicad_tools.pcb import get_footprint_pads, FootprintLibrary

    # Get pads for a footprint
    pads = get_footprint_pads("Capacitor_SMD:C_0603_1608Metric")
    # Returns: {"1": (-0.775, 0), "2": (0.775, 0)}

    # Or use library directly
    lib = FootprintLibrary()
    pads = lib.get_pads("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm")
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PadInfo:
    """Information about a footprint pad."""

    name: str
    x: float
    y: float
    width: float = 0
    height: float = 0
    shape: str = "roundrect"
    layers: tuple = ("F.Cu", "F.Mask", "F.Paste")


# =============================================================================
# Built-in Footprint Pad Positions
# =============================================================================

# These are the actual pad positions from KiCad's standard footprint libraries.
# Having them built-in ensures reproducibility and avoids library path issues.

COMMON_FOOTPRINTS = {
    # -------------------------------------------------------------------------
    # Passive Components
    # -------------------------------------------------------------------------
    "Capacitor_SMD:C_0402_1005Metric": {
        "1": (-0.48, 0),
        "2": (0.48, 0),
    },
    "Capacitor_SMD:C_0603_1608Metric": {
        "1": (-0.775, 0),
        "2": (0.775, 0),
    },
    "Capacitor_SMD:C_0805_2012Metric": {
        "1": (-0.95, 0),
        "2": (0.95, 0),
    },
    "Resistor_SMD:R_0402_1005Metric": {
        "1": (-0.48, 0),
        "2": (0.48, 0),
    },
    "Resistor_SMD:R_0603_1608Metric": {
        "1": (-0.775, 0),
        "2": (0.775, 0),
    },
    "Inductor_SMD:L_0603_1608Metric": {
        "1": (-0.775, 0),
        "2": (0.775, 0),
    },
    "LED_SMD:LED_0603_1608Metric": {
        # Pin 1 = Cathode (K), Pin 2 = Anode (A)
        "1": (-0.775, 0),
        "2": (0.775, 0),
    },
    "Diode_SMD:D_SOD-323": {
        "1": (-1.25, 0),
        "2": (1.25, 0),
    },
    # -------------------------------------------------------------------------
    # Voltage Regulators
    # -------------------------------------------------------------------------
    "Package_TO_SOT_SMD:SOT-23-5": {
        # Typical LDO pinout (e.g., XC6206, AP2112):
        # Pin 1: VIN (left, bottom)
        # Pin 2: GND (left, middle)
        # Pin 3: EN (left, top)
        # Pin 4: NC/BYPASS (right, top)
        # Pin 5: VOUT (right, bottom)
        "1": (-1.1375, -0.95),
        "2": (-1.1375, 0),
        "3": (-1.1375, 0.95),
        "4": (1.1375, 0.95),
        "5": (1.1375, -0.95),
    },
    # -------------------------------------------------------------------------
    # MCU - STM32C011 (TSSOP-20)
    # -------------------------------------------------------------------------
    "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm": {
        # Left side (pins 1-10, top to bottom in schematic = bottom to top physically)
        "1": (-2.8625, -2.925),
        "2": (-2.8625, -2.275),
        "3": (-2.8625, -1.625),
        "4": (-2.8625, -0.975),  # VDD
        "5": (-2.8625, -0.325),  # VSS
        "6": (-2.8625, 0.325),
        "7": (-2.8625, 0.975),
        "8": (-2.8625, 1.625),
        "9": (-2.8625, 2.275),
        "10": (-2.8625, 2.925),
        # Right side (pins 11-20, bottom to top physically)
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
    # -------------------------------------------------------------------------
    # DAC - PCM5102A (TSSOP-28)
    # -------------------------------------------------------------------------
    "Package_SO:TSSOP-28_4.4x9.7mm_P0.65mm": {
        # Pins 1-14 on left, 15-28 on right
        # 0.65mm pitch, starting from center
        **{str(i): (-2.8625, -4.225 + (i - 1) * 0.65) for i in range(1, 15)},
        **{str(i): (2.8625, 4.225 - (i - 15) * 0.65) for i in range(15, 29)},
    },
    # -------------------------------------------------------------------------
    # Oscillator - 3.2x2.5mm SMD
    # -------------------------------------------------------------------------
    "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm": {
        # Standard 4-pin oscillator pinout:
        # Pin 1: EN (enable)
        # Pin 2: GND
        # Pin 3: OUT (clock output)
        # Pin 4: VDD
        "1": (-1.05, 0.825),
        "2": (1.05, 0.825),
        "3": (1.05, -0.825),
        "4": (-1.05, -0.825),
    },
    # -------------------------------------------------------------------------
    # Connectors
    # -------------------------------------------------------------------------
    "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical": {
        # 40-pin Raspberry Pi header (2 rows x 20 pins)
        # Odd pins on left (1,3,5...), even on right (2,4,6...)
        # 2.54mm pitch
        **{str(i): (-1.27, -24.13 + ((i - 1) // 2) * 2.54) for i in range(1, 40, 2)},
        **{str(i): (1.27, -24.13 + ((i - 2) // 2) * 2.54) for i in range(2, 41, 2)},
    },
    "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical": {
        # 4-pin debug header (SWD)
        "1": (0, 0),
        "2": (0, 2.54),
        "3": (0, 5.08),
        "4": (0, 7.62),
    },
    "Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal": {
        # 3.5mm audio jack
        # Tip (L), Ring (R), Sleeve (GND)
        "T": (-3.3, 0),
        "R": (3.3, 0),
        "S": (0, 5.0),
    },
}

# Aliases for short footprint names (without library prefix)
FOOTPRINT_ALIASES = {
    "C_0402_1005Metric": "Capacitor_SMD:C_0402_1005Metric",
    "C_0603_1608Metric": "Capacitor_SMD:C_0603_1608Metric",
    "C_0805_2012Metric": "Capacitor_SMD:C_0805_2012Metric",
    "R_0402_1005Metric": "Resistor_SMD:R_0402_1005Metric",
    "R_0603_1608Metric": "Resistor_SMD:R_0603_1608Metric",
    "L_0603_1608Metric": "Inductor_SMD:L_0603_1608Metric",
    "LED_0603_1608Metric": "LED_SMD:LED_0603_1608Metric",
    "D_SOD-323": "Diode_SMD:D_SOD-323",
    "SOT-23-5": "Package_TO_SOT_SMD:SOT-23-5",
    "TSSOP-20_4.4x6.5mm_P0.65mm": "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
    "TSSOP-28_4.4x9.7mm_P0.65mm": "Package_SO:TSSOP-28_4.4x9.7mm_P0.65mm",
    "Oscillator_SMD_3.2x2.5mm": "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
    "PinSocket_2x20_P2.54mm": "Connector_PinSocket_2.54mm:PinSocket_2x20_P2.54mm_Vertical",
    "PinHeader_1x04_P2.54mm": "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical",
    "Jack_3.5mm_Horizontal": "Connector_Audio:Jack_3.5mm_CUI_SJ-3523-SMT_Horizontal",
}


# =============================================================================
# Footprint Library Reader
# =============================================================================


class FootprintLibrary:
    """
    Reads footprint pad positions from KiCad libraries.

    Falls back to hardcoded data for known footprints if library
    files are not available.
    """

    # Default KiCad library paths
    KICAD_PATHS = [
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        "/usr/share/kicad/footprints",
        os.path.expanduser("~/Library/Application Support/kicad/8.0/footprints"),
    ]

    def __init__(self, library_paths: list[str] = None):
        """
        Initialize footprint library.

        Args:
            library_paths: List of paths to search for .pretty directories.
                          If None, uses default KiCad paths.
        """
        self.library_paths = library_paths or self.KICAD_PATHS
        self._cache: dict[str, dict[str, tuple]] = {}

    def _find_footprint_file(self, lib_name: str, fp_name: str) -> Optional[Path]:
        """Find the .kicad_mod file for a footprint."""
        pretty_dir = f"{lib_name}.pretty"
        mod_file = f"{fp_name}.kicad_mod"

        for base_path in self.library_paths:
            fp_path = Path(base_path) / pretty_dir / mod_file
            if fp_path.exists():
                return fp_path

        return None

    def _parse_footprint_file(self, filepath: Path) -> dict[str, tuple]:
        """Parse a .kicad_mod file and extract pad positions."""
        content = filepath.read_text()
        pads = {}

        # Pattern to match pad definitions
        # (pad "1" smd roundrect (at -0.775 0) ...)
        pad_pattern = r'\(pad\s+"([^"]+)"\s+\w+\s+\w+\s*\(at\s+([-\d.]+)\s+([-\d.]+)'

        for match in re.finditer(pad_pattern, content):
            pad_name = match.group(1)
            x = float(match.group(2))
            y = float(match.group(3))
            pads[pad_name] = (x, y)

        return pads

    def get_pads(self, footprint: str) -> dict[str, tuple]:
        """
        Get pad positions for a footprint.

        Args:
            footprint: Full footprint name (e.g., "Capacitor_SMD:C_0603_1608Metric")
                      or short name (e.g., "C_0603_1608Metric")

        Returns:
            Dict mapping pad name to (x, y) position relative to footprint center.
        """
        # Check cache first
        if footprint in self._cache:
            return self._cache[footprint]

        # Resolve aliases
        resolved = FOOTPRINT_ALIASES.get(footprint, footprint)

        # Check built-in data
        if resolved in COMMON_FOOTPRINTS:
            pads = COMMON_FOOTPRINTS[resolved]
            self._cache[footprint] = pads
            return pads

        # Try to parse from library file
        if ":" in resolved:
            lib_name, fp_name = resolved.split(":", 1)
            fp_path = self._find_footprint_file(lib_name, fp_name)
            if fp_path:
                pads = self._parse_footprint_file(fp_path)
                self._cache[footprint] = pads
                return pads

        # Fallback for unknown footprints - assume simple 2-pad component
        print(f"Warning: Unknown footprint '{footprint}', using default 2-pad layout")
        pads = {"1": (-0.5, 0), "2": (0.5, 0)}
        self._cache[footprint] = pads
        return pads

    def list_known_footprints(self) -> list[str]:
        """List all footprints with built-in data."""
        return list(COMMON_FOOTPRINTS.keys())


# =============================================================================
# Module-level convenience functions
# =============================================================================

_default_library: Optional[FootprintLibrary] = None


def get_library() -> FootprintLibrary:
    """Get the default footprint library instance."""
    global _default_library
    if _default_library is None:
        _default_library = FootprintLibrary()
    return _default_library


def get_footprint_pads(footprint: str) -> dict[str, tuple]:
    """
    Get pad positions for a footprint.

    Args:
        footprint: Footprint name (full or short form)

    Returns:
        Dict mapping pad name to (x, y) position.
    """
    return get_library().get_pads(footprint)


# =============================================================================
# STM32C011 Pin Mapping
# =============================================================================

# Map schematic pin names to physical pin numbers for STM32C011F4P6 (TSSOP-20)
STM32C011_PIN_MAP = {
    # Power
    "VDD": "4",
    "VSS": "5",
    # Port A
    "PA0": "7",
    "PA1": "8",
    "PA2": "9",
    "PA3": "10",
    "PA4": "11",
    "PA5": "12",
    "PA6": "13",
    "PA7": "14",
    "PA8": "15",
    "PA11": "16",
    "PA12": "17",
    "PA13": "18",  # SWDIO
    "PA14": "19",  # SWCLK
    "PA15": "20",
    # Port B
    "PB6": "1",
    "PB7": "2",
    # Port C
    "PC14": "3",
    "PC15": "6",
    # Port F
    "PF2": "6",  # Alternate function
}


def get_stm32c011_pad(pin_name: str) -> tuple[float, float]:
    """
    Get pad position for an STM32C011 pin by name.

    Args:
        pin_name: Pin name (e.g., "PA0", "VDD", "SWDIO")

    Returns:
        (x, y) position relative to footprint center.
    """
    # Handle SWDIO/SWCLK aliases
    if pin_name == "SWDIO":
        pin_name = "PA13"
    elif pin_name == "SWCLK":
        pin_name = "PA14"

    if pin_name not in STM32C011_PIN_MAP:
        raise KeyError(f"Unknown STM32C011 pin: {pin_name}")

    pin_num = STM32C011_PIN_MAP[pin_name]
    pads = get_footprint_pads("Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm")
    return pads[pin_num]


# =============================================================================
# Demo / Test
# =============================================================================

if __name__ == "__main__":
    print("KiCad Footprint Reader")
    print("=" * 60)

    lib = FootprintLibrary()

    # Test known footprints
    test_footprints = [
        "Capacitor_SMD:C_0603_1608Metric",
        "Package_TO_SOT_SMD:SOT-23-5",
        "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
        "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
    ]

    for fp in test_footprints:
        print(f"\n{fp}:")
        pads = lib.get_pads(fp)
        for name, pos in sorted(pads.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 999):
            print(f"  Pad {name}: ({pos[0]:7.3f}, {pos[1]:7.3f})")

    # Test STM32 pin mapping
    print("\n" + "=" * 60)
    print("STM32C011 Pin Positions:")
    for pin in ["VDD", "VSS", "PA0", "PA13", "SWDIO"]:
        pos = get_stm32c011_pad(pin)
        print(f"  {pin:6}: ({pos[0]:7.3f}, {pos[1]:7.3f})")

    # Test short names
    print("\n" + "=" * 60)
    print("Short name resolution:")
    for short in ["C_0603_1608Metric", "SOT-23-5", "TSSOP-20_4.4x6.5mm_P0.65mm"]:
        resolved = FOOTPRINT_ALIASES.get(short, short)
        print(f"  {short} -> {resolved}")
