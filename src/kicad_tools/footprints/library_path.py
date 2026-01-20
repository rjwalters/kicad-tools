"""KiCad library path detection utilities.

Detects the location of KiCad's standard footprint libraries on different platforms.
"""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

# Default library paths for each platform
_KICAD_LIBRARY_PATHS = {
    "Darwin": [  # macOS
        # KiCad 8.x standard installation
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        Path.home() / "Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        # KiCad versioned installations (e.g., KiCad 8.0)
        "/Applications/KiCad/kicad.app/Contents/SharedSupport/footprints",
        # User-specific KiCad libraries
        Path.home() / "Library/Application Support/kicad/footprints",
        Path.home() / ".local/share/kicad/8.0/footprints",
        Path.home() / ".local/share/kicad/footprints",
        # Homebrew installation
        "/opt/homebrew/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
    ],
    "Linux": [
        "/usr/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
        Path.home() / ".local/share/kicad/footprints",
        # KiCad 8.x versioned user paths
        Path.home() / ".local/share/kicad/8.0/footprints",
        # Flatpak
        Path.home() / ".var/app/org.kicad.KiCad/data/kicad/footprints",
    ],
    "Windows": [
        Path("C:/Program Files/KiCad/share/kicad/footprints"),
        Path("C:/Program Files (x86)/KiCad/share/kicad/footprints"),
        Path.home() / "AppData/Local/Programs/KiCad/share/kicad/footprints",
        # KiCad 8.x versioned paths
        Path("C:/Program Files/KiCad/8.0/share/kicad/footprints"),
        Path.home() / "Documents/KiCad/8.0/footprints",
    ],
}

# Standard library mappings: footprint name patterns -> library directories
# These are common footprint libraries in KiCad's standard installation
STANDARD_LIBRARY_MAPPINGS = {
    # Capacitors
    "C_": "Capacitor_SMD.pretty",
    "CP_": "Capacitor_SMD.pretty",
    # Resistors
    "R_": "Resistor_SMD.pretty",
    # Inductors
    "L_": "Inductor_SMD.pretty",
    # LEDs
    "LED_": "LED_SMD.pretty",
    # Crystals
    "Crystal_": "Crystal.pretty",
    # Connectors
    "Conn_": "Connector_PinHeader_2.54mm.pretty",
    "PinHeader_": "Connector_PinHeader_2.54mm.pretty",
    "USB_": "Connector_USB.pretty",
    # ICs and packages
    "SOIC-": "Package_SO.pretty",
    "SOP-": "Package_SO.pretty",
    "SSOP-": "Package_SO.pretty",
    "TSSOP-": "Package_SO.pretty",
    "QFP-": "Package_QFP.pretty",
    "QFN-": "Package_DFN_QFN.pretty",
    "DFN-": "Package_DFN_QFN.pretty",
    "BGA-": "Package_BGA.pretty",
    "SOT-": "Package_TO_SOT_SMD.pretty",
    "TO-": "Package_TO_SOT_SMD.pretty",
    "LQFP-": "Package_QFP.pretty",
}


@dataclass
class LibraryPaths:
    """Container for KiCad library paths."""

    footprints_path: Path | None
    """Path to the footprints directory, or None if not found."""

    source: str
    """Where the path came from: 'auto', 'config', or 'env'."""

    @property
    def found(self) -> bool:
        """Whether a valid footprints path was found."""
        return self.footprints_path is not None and self.footprints_path.exists()

    def get_library_path(self, library_name: str) -> Path | None:
        """Get path to a specific footprint library directory.

        Args:
            library_name: Library name with or without .pretty extension
                          (e.g., "Capacitor_SMD" or "Capacitor_SMD.pretty")

        Returns:
            Path to the library directory if found, None otherwise.
        """
        if not self.footprints_path:
            return None

        # Ensure .pretty extension
        if not library_name.endswith(".pretty"):
            library_name = f"{library_name}.pretty"

        lib_path = self.footprints_path / library_name
        if lib_path.exists() and lib_path.is_dir():
            return lib_path

        return None

    def get_footprint_file(
        self, library_name: str, footprint_name: str, fallback_search: bool = True
    ) -> Path | None:
        """Get path to a specific footprint file.

        Args:
            library_name: Library name (e.g., "Capacitor_SMD")
            footprint_name: Footprint name (e.g., "C_0402_1005Metric")
            fallback_search: If True and the library isn't found, search all
                           available libraries for the footprint (default: True)

        Returns:
            Path to the .kicad_mod file if found, None otherwise.
        """
        lib_path = self.get_library_path(library_name)

        # Ensure .kicad_mod extension for filename
        fp_filename = footprint_name
        if not fp_filename.endswith(".kicad_mod"):
            fp_filename = f"{fp_filename}.kicad_mod"

        if lib_path:
            fp_path = lib_path / fp_filename
            if fp_path.exists():
                return fp_path

        # If library wasn't found or footprint not in library, try fallback search
        if fallback_search:
            result = self.find_footprint_by_name(footprint_name)
            if result:
                return result

        return None

    def find_footprint_by_name(self, footprint_name: str) -> Path | None:
        """Search all available libraries for a footprint by name.

        This is a fallback search when the explicitly named library isn't found.
        Useful for finding footprints when library paths differ between KiCad versions.

        Args:
            footprint_name: Footprint name (e.g., "Converter_ACDC_Hi-Link_HLK-PMxx")

        Returns:
            Path to the .kicad_mod file if found, None otherwise.
        """
        if not self.footprints_path or not self.footprints_path.exists():
            return None

        # Ensure .kicad_mod extension
        fp_filename = footprint_name
        if not fp_filename.endswith(".kicad_mod"):
            fp_filename = f"{fp_filename}.kicad_mod"

        # Search all .pretty directories
        for lib_dir in self.footprints_path.iterdir():
            if lib_dir.is_dir() and lib_dir.name.endswith(".pretty"):
                fp_path = lib_dir / fp_filename
                if fp_path.exists():
                    return fp_path

        return None


def detect_kicad_library_path(config_override: str | Path | None = None) -> LibraryPaths:
    """Detect the KiCad footprint library path.

    Checks in order:
    1. Explicit config override
    2. KICAD_FOOTPRINT_DIR environment variable
    3. Platform-specific default locations

    Args:
        config_override: Optional explicit path from configuration

    Returns:
        LibraryPaths with the detected path and source information.
    """
    # 1. Check config override
    if config_override:
        path = Path(config_override)
        if path.exists():
            return LibraryPaths(footprints_path=path, source="config")

    # 2. Check environment variable
    env_path = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return LibraryPaths(footprints_path=path, source="env")

    # 3. Check platform-specific defaults
    system = platform.system()
    default_paths = _KICAD_LIBRARY_PATHS.get(system, [])

    for path in default_paths:
        path = Path(path)
        if path.exists() and path.is_dir():
            return LibraryPaths(footprints_path=path, source="auto")

    # Not found
    return LibraryPaths(footprints_path=None, source="auto")


def guess_standard_library(footprint_name: str) -> str | None:
    """Guess the standard library name for a footprint.

    Uses naming conventions to guess which KiCad standard library
    a footprint belongs to.

    Args:
        footprint_name: The footprint name (e.g., "C_0402_1005Metric")

    Returns:
        Library name (e.g., "Capacitor_SMD") if guessed, None otherwise.
    """
    for prefix, library in STANDARD_LIBRARY_MAPPINGS.items():
        if footprint_name.startswith(prefix):
            # Return without .pretty extension
            return library.removesuffix(".pretty")

    return None


def parse_library_id(lib_id: str) -> tuple[str | None, str]:
    """Parse a full library ID into library name and footprint name.

    KiCad library IDs can be in format "Library:FootprintName" or just "FootprintName".

    Args:
        lib_id: The library ID (e.g., "Capacitor_SMD:C_0402_1005Metric")

    Returns:
        Tuple of (library_name, footprint_name). library_name may be None.
    """
    if ":" in lib_id:
        library, footprint = lib_id.split(":", 1)
        return library, footprint
    return None, lib_id


def list_available_libraries(paths: LibraryPaths) -> list[str]:
    """List all available footprint libraries.

    Args:
        paths: LibraryPaths object with detected paths

    Returns:
        List of library names (without .pretty extension).
    """
    if not paths.footprints_path or not paths.footprints_path.exists():
        return []

    libraries = []
    for item in paths.footprints_path.iterdir():
        if item.is_dir() and item.name.endswith(".pretty"):
            libraries.append(item.name.removesuffix(".pretty"))

    return sorted(libraries)
