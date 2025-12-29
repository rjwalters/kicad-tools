"""
PCB Manufacturer Profiles Package.

Provides manufacturer-specific design rules, assembly capabilities,
and parts library information for various PCB fabrication houses.

Supported manufacturers:
- JLCPCB (jlcpcb) - Chinese fab with LCSC parts library
- Seeed Fusion (seeed) - Chinese fab with OPL parts library
- PCBWay (pcbway) - Chinese fab with global sourcing
- OSHPark (oshpark) - US fab, PCB only, per-sq-inch pricing

Usage:
    from manufacturers import get_profile, list_manufacturers

    # Get a specific manufacturer
    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    # List all manufacturers
    for mfr in list_manufacturers():
        print(f"{mfr.name}: {mfr.website}")
"""

from typing import Optional

from .base import (
    AssemblyCapabilities,
    DesignRules,
    ManufacturerProfile,
    PartsLibrary,
)
from .jlcpcb import JLCPCB_PROFILE
from .oshpark import OSHPARK_PROFILE
from .pcbway import PCBWAY_PROFILE
from .seeed import SEEED_PROFILE

__all__ = [
    # Base classes
    "DesignRules",
    "AssemblyCapabilities",
    "PartsLibrary",
    "ManufacturerProfile",
    # Functions
    "get_profile",
    "list_manufacturers",
    "get_manufacturer_ids",
    # Profiles (for direct access)
    "JLCPCB_PROFILE",
    "SEEED_PROFILE",
    "PCBWAY_PROFILE",
    "OSHPARK_PROFILE",
]

# Registry of all manufacturer profiles
_PROFILES: dict[str, ManufacturerProfile] = {
    "jlcpcb": JLCPCB_PROFILE,
    "seeed": SEEED_PROFILE,
    "pcbway": PCBWAY_PROFILE,
    "oshpark": OSHPARK_PROFILE,
}

# Aliases for convenience
_ALIASES: dict[str, str] = {
    "jlc": "jlcpcb",
    "lcsc": "jlcpcb",
    "seeed_fusion": "seeed",
    "seeedstudio": "seeed",
    "osh": "oshpark",
    "osh_park": "oshpark",
}


def get_profile(manufacturer_id: str) -> ManufacturerProfile:
    """
    Get a manufacturer profile by ID.

    Args:
        manufacturer_id: Manufacturer identifier (e.g., "jlcpcb", "seeed")

    Returns:
        ManufacturerProfile for the specified manufacturer

    Raises:
        ValueError: If manufacturer_id is not recognized
    """
    # Normalize ID
    normalized = manufacturer_id.lower().strip()

    # Check aliases
    if normalized in _ALIASES:
        normalized = _ALIASES[normalized]

    # Get profile
    if normalized not in _PROFILES:
        available = ", ".join(sorted(_PROFILES.keys()))
        raise ValueError(f"Unknown manufacturer: {manufacturer_id!r}. Available: {available}")

    return _PROFILES[normalized]


def list_manufacturers() -> list[ManufacturerProfile]:
    """
    Get list of all available manufacturer profiles.

    Returns:
        List of ManufacturerProfile objects
    """
    return list(_PROFILES.values())


def get_manufacturer_ids() -> list[str]:
    """
    Get list of valid manufacturer IDs.

    Returns:
        List of manufacturer ID strings
    """
    return sorted(_PROFILES.keys())


def compare_design_rules(
    layers: int = 4,
    copper_oz: float = 1.0,
    manufacturers: Optional[list[str]] = None,
) -> dict[str, DesignRules]:
    """
    Compare design rules across manufacturers.

    Args:
        layers: Layer count (2, 4, 6, etc.)
        copper_oz: Copper weight in oz
        manufacturers: List of manufacturer IDs (default: all)

    Returns:
        Dict mapping manufacturer ID to DesignRules
    """
    if manufacturers is None:
        manufacturers = list(_PROFILES.keys())

    results = {}
    for mfr_id in manufacturers:
        profile = get_profile(mfr_id)
        results[mfr_id] = profile.get_design_rules(layers, copper_oz)

    return results


def find_compatible_manufacturers(
    trace_width_mm: float,
    clearance_mm: float,
    via_drill_mm: float,
    layers: int = 4,
    needs_assembly: bool = False,
) -> list[ManufacturerProfile]:
    """
    Find manufacturers that can meet specified design constraints.

    Args:
        trace_width_mm: Minimum trace width in design
        clearance_mm: Minimum clearance in design
        via_drill_mm: Minimum via drill in design
        layers: Required layer count
        needs_assembly: Whether PCBA service is needed

    Returns:
        List of compatible ManufacturerProfiles
    """
    compatible = []

    for profile in _PROFILES.values():
        # Check layer support
        if layers not in profile.supported_layers:
            continue

        # Check assembly requirement
        if needs_assembly and not profile.supports_assembly():
            continue

        # Check design rules
        rules = profile.get_design_rules(layers)
        if (
            trace_width_mm >= rules.min_trace_width_mm
            and clearance_mm >= rules.min_clearance_mm
            and via_drill_mm >= rules.min_via_drill_mm
        ):
            compatible.append(profile)

    return compatible
