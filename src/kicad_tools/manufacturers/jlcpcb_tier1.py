"""
JLCPCB Capability Plus (tier 1) manufacturer profile.

This profile represents JLCPCB's advanced "Capability Plus" tier, which
supports via-in-pad with epoxy fill plus plating over (typically a
per-order surcharge of ~$30 in 2026). The scalar DRC limits otherwise
mirror the standard JLCPCB advanced PCB process.

Assembly capability and parts library are identical to standard JLCPCB
and are reused via imports from :mod:`kicad_tools.manufacturers.jlcpcb`.

The router-side equivalent is
:data:`kicad_tools.router.mfr_limits.MFR_JLCPCB_TIER1`, which carries
``via_in_pad_supported=True``. The two registries (router and DRC) are
kept in sync via tests; see ``tests/test_manufacturer_registry_sync.py``.

Source: https://jlcpcb.com/capabilities/pcb-capabilities
        https://jlcpcb.com/capabilities/Capabilities (Capability Plus tier)
"""

from .base import (
    ManufacturerProfile,
    load_design_rules_from_yaml,
)
from .jlcpcb import _ROTATION_CORRECTIONS, JLCPCB_ASSEMBLY, LCSC_LIBRARY

# Load design rules from YAML configuration
_DESIGN_RULES = load_design_rules_from_yaml("jlcpcb_tier1")

# Complete JLCPCB Capability Plus (tier 1) Profile
JLCPCB_TIER1_PROFILE = ManufacturerProfile(
    id="jlcpcb-tier1",
    name="JLCPCB Capability Plus",
    website="https://jlcpcb.com",
    design_rules=_DESIGN_RULES,
    assembly=JLCPCB_ASSEMBLY,
    parts_library=LCSC_LIBRARY,
    lead_times={
        "pcb_standard": 5,
        "pcb_expedited": 2,
        "pcba_basic": 7,
        "pcba_extended": 14,
        "pcba_global": 21,
    },
    bom_format="jlcpcb",
    supported_layers=[1, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20],
    pricing_model="per_pcb",
    rotation_corrections=_ROTATION_CORRECTIONS,
    pnp_format_id="jlcpcb",
    gerber_preset_id="jlcpcb",
)


def get_profile() -> ManufacturerProfile:
    """Get the JLCPCB Capability Plus (tier 1) manufacturer profile."""
    return JLCPCB_TIER1_PROFILE
