"""
PCB Manufacturer Profiles Package.

Provides manufacturer-specific design rules, assembly capabilities,
and parts library information for various PCB fabrication houses.

Supported manufacturers:
- JLCPCB (jlcpcb) - Chinese fab with LCSC parts library
- JLCPCB Capability Plus (jlcpcb-tier1) - JLCPCB advanced tier with via-in-pad
- Seeed Fusion (seeed) - Chinese fab with OPL parts library
- PCBWay (pcbway) - Chinese fab with global sourcing
- OSHPark (oshpark) - US fab, PCB only, per-sq-inch pricing
- FlashPCB (flashpcb) - US fab with assembly services

Usage:
    from manufacturers import get_profile, list_manufacturers

    # Get a specific manufacturer
    profile = get_profile("jlcpcb")
    rules = profile.get_design_rules(layers=4, copper_oz=1.0)

    # List all manufacturers
    for mfr in list_manufacturers():
        print(f"{mfr.name}: {mfr.website}")
"""

from .base import (
    AssemblyCapabilities,
    DesignRules,
    FileNamingConvention,
    ManufacturerProfile,
    PartsLibrary,
    load_design_rules_from_yaml,
    load_rotation_corrections,
    match_rotation_correction,
)
from .dru_generator import generate_dru
from .project_generator import (
    build_default_netclass,
    build_project_data,
    build_project_rules,
    write_drc_constraints,
)
from .flashpcb import FLASHPCB_PROFILE
from .jlcpcb import JLCPCB_PROFILE
from .jlcpcb_tier1 import JLCPCB_TIER1_PROFILE
from .oshpark import OSHPARK_PROFILE
from .pcbway import PCBWAY_PROFILE
from .seeed import SEEED_PROFILE

__all__ = [
    # Base classes
    "DesignRules",
    "AssemblyCapabilities",
    "PartsLibrary",
    "ManufacturerProfile",
    "FileNamingConvention",
    # Functions
    "get_profile",
    "get_fab_family",
    "list_manufacturers",
    "get_manufacturer_ids",
    "get_all_manufacturer_names",
    "load_design_rules_from_yaml",
    "load_rotation_corrections",
    "match_rotation_correction",
    "generate_dru",
    "build_default_netclass",
    "build_project_data",
    "build_project_rules",
    "write_drc_constraints",
    # Profiles (for direct access)
    "FLASHPCB_PROFILE",
    "JLCPCB_PROFILE",
    "JLCPCB_TIER1_PROFILE",
    "SEEED_PROFILE",
    "PCBWAY_PROFILE",
    "OSHPARK_PROFILE",
]

# Registry of all manufacturer profiles
_PROFILES: dict[str, ManufacturerProfile] = {
    "flashpcb": FLASHPCB_PROFILE,
    "jlcpcb": JLCPCB_PROFILE,
    "jlcpcb-tier1": JLCPCB_TIER1_PROFILE,
    "seeed": SEEED_PROFILE,
    "pcbway": PCBWAY_PROFILE,
    "oshpark": OSHPARK_PROFILE,
}

# Aliases for convenience
#
# The jlcpcb-tier1 aliases below mirror the router's
# ``src/kicad_tools/router/mfr_limits.py:_MFR_ALIASES`` table verbatim
# so the router and DRC registries cannot drift. See
# ``tests/test_manufacturer_registry_sync.py`` for the invariant check.
_ALIASES: dict[str, str] = {
    "flash": "flashpcb",
    "jlc": "jlcpcb",
    "lcsc": "jlcpcb",
    "seeed_fusion": "seeed",
    "seeed-fusion": "seeed",  # mirror router/mfr_limits.MFR_LIMITS canonical key
    "seeedfusion": "seeed",  # mirror router/mfr_limits._MFR_ALIASES
    "seeedstudio": "seeed",
    "osh": "oshpark",
    "osh_park": "oshpark",
    # JLCPCB Capability Plus (tier 1) - mirror router's _MFR_ALIASES
    "jlcpcb_tier1": "jlcpcb-tier1",
    "jlcpcb-capabilityplus": "jlcpcb-tier1",
    "jlcpcb_capabilityplus": "jlcpcb-tier1",
    "jlcpcb-capability-plus": "jlcpcb-tier1",
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


# Capability-tier profiles map to a parent *fab family* for export-format
# purposes.  A tier profile (e.g. ``jlcpcb-tier1``) changes DRC limits but
# the physical fab -- and therefore the BOM/CPL CSV format, Gerber naming
# preset, LCSC parts library, and output filenames -- is the parent fab.
_FAB_FAMILY: dict[str, str] = {
    "jlcpcb-tier1": "jlcpcb",
}


def get_fab_family(manufacturer_id: str) -> str:
    """Resolve a manufacturer ID to its fab *family* for export formats.

    Capability tiers of the same physical fab (e.g. ``jlcpcb-tier1``)
    share the parent fab's BOM/CPL CSV formats, Gerber filename preset,
    parts library (LCSC), and output file naming.  Export-format code
    should select formatters by family while DRC/audit/report code keeps
    the full profile ID so capability differences (via-in-pad, finer
    trace classes) are honored.  See issue #3497: exporting at
    ``--mfr jlcpcb-tier1`` must produce JLCPCB-format outputs while the
    report's DRC section runs against the tier1 rules.

    Args:
        manufacturer_id: Manufacturer identifier or alias
            (e.g. ``"jlcpcb-tier1"``, ``"jlcpcb_tier1"``, ``"jlc"``).

    Returns:
        Canonical family ID (e.g. ``"jlcpcb"``).  Unrecognized IDs are
        returned normalized (lowercased/stripped) so callers like the
        ``generic`` export path keep working.
    """
    normalized = manufacturer_id.lower().strip()
    if normalized in _ALIASES:
        normalized = _ALIASES[normalized]
    return _FAB_FAMILY.get(normalized, normalized)


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


def get_all_manufacturer_names() -> list[str]:
    """
    Get every name accepted by ``get_profile`` -- canonical IDs and aliases.

    Returns the union of ``_PROFILES`` keys (canonical names) and
    ``_ALIASES`` keys (alternate spellings such as ``jlc``, ``lcsc``,
    ``jlcpcb_tier1``, ``jlcpcb-capabilityplus``, ...). This is the
    correct ``choices=`` source for argparse ``--mfr`` flags: it accepts
    every spelling the registry resolves, mirroring the router's CLI
    behaviour.

    See issue #2793 for why ``get_manufacturer_ids()`` alone is not
    sufficient (it returns canonicals only, silently rejecting valid
    aliases such as ``jlcpcb_tier1`` at argparse-time before
    ``get_profile()`` is ever called).

    Returns:
        Sorted list of every manufacturer name and alias.
    """
    return sorted(set(_PROFILES.keys()) | set(_ALIASES.keys()))


def compare_design_rules(
    layers: int = 4,
    copper_oz: float = 1.0,
    manufacturers: list[str] | None = None,
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
