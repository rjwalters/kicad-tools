"""
Thermal awareness for placement optimization.

Provides thermal classification of components and constraints to ensure
power components are placed for adequate heat dissipation and temperature-sensitive
components are kept away from heat sources.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB

__all__ = [
    "ThermalClass",
    "ThermalProperties",
    "ThermalConstraint",
    "classify_thermal_properties",
    "detect_thermal_constraints",
    "ThermalConfig",
]


class ThermalClass(Enum):
    """Classification of components by thermal behavior."""

    HEAT_SOURCE = "heat_source"  # LDOs, MOSFETs, power resistors
    HEAT_SENSITIVE = "heat_sensitive"  # Crystals, precision refs
    NEUTRAL = "neutral"  # Most components


@dataclass
class ThermalProperties:
    """Thermal properties for a component."""

    thermal_class: ThermalClass = ThermalClass.NEUTRAL
    power_dissipation_w: float = 0.0  # Typical power dissipation
    max_temp_c: float = 85.0  # Maximum operating temperature
    thermal_sensitivity: str = "none"  # "high", "medium", "low", "none"
    needs_thermal_relief: bool = False  # Requires thermal vias/pour


@dataclass
class ThermalConstraint:
    """A thermal placement constraint."""

    constraint_type: str  # "min_separation", "edge_preference", "thermal_zone"
    parameters: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        return f"ThermalConstraint({self.constraint_type}, {self.parameters})"


@dataclass
class ThermalConfig:
    """Configuration for thermal-aware placement."""

    # Minimum separation between heat sources and sensitive components (mm)
    heat_source_separation_mm: float = 15.0

    # Maximum distance from board edge for heat sources (mm)
    edge_preference_max_mm: float = 10.0

    # Repulsion strength between heat sources and sensitive components
    thermal_repulsion_strength: float = 500.0

    # Edge attraction strength for heat sources
    edge_attraction_strength: float = 50.0

    # Enable thermal zone grouping for power components
    enable_thermal_zones: bool = True


def classify_thermal_properties(
    pcb: PCB,
) -> dict[str, ThermalProperties]:
    """
    Classify components by thermal characteristics.

    Uses heuristics based on reference designator, footprint type, and value
    to determine thermal classification.

    Args:
        pcb: Loaded PCB object

    Returns:
        Dictionary mapping reference designator to ThermalProperties
    """
    properties: dict[str, ThermalProperties] = {}

    for fp in pcb.footprints:
        ref = fp.reference
        value = getattr(fp, "value", "") or ""
        # Try footprint_library_id first, then fall back to name
        footprint_name = getattr(fp, "footprint_library_id", None) or getattr(fp, "name", "") or ""

        # Classify based on reference prefix and value
        thermal = _classify_component(ref, value, footprint_name, fp)
        properties[ref] = thermal

    return properties


def _classify_component(ref: str, value: str, footprint_name: str, fp) -> ThermalProperties:
    """Classify a single component's thermal properties."""
    ref_prefix = "".join(c for c in ref if c.isalpha()).upper()
    value_lower = value.lower()
    footprint_lower = footprint_name.lower()

    # Heat sources: LDOs, regulators, power MOSFETs, power resistors
    if _is_heat_source(ref_prefix, value_lower, footprint_lower, fp):
        return ThermalProperties(
            thermal_class=ThermalClass.HEAT_SOURCE,
            power_dissipation_w=_estimate_power_dissipation(ref_prefix, value, fp),
            max_temp_c=125.0,
            thermal_sensitivity="none",
            needs_thermal_relief=_needs_thermal_relief(footprint_lower, fp),
        )

    # Heat sensitive: crystals, precision voltage references, temp sensors
    if _is_heat_sensitive(ref_prefix, value_lower, footprint_lower):
        return ThermalProperties(
            thermal_class=ThermalClass.HEAT_SENSITIVE,
            power_dissipation_w=0.0,
            max_temp_c=70.0,
            thermal_sensitivity="high",
            needs_thermal_relief=False,
        )

    # Default: neutral
    return ThermalProperties(
        thermal_class=ThermalClass.NEUTRAL,
        power_dissipation_w=0.0,
        max_temp_c=85.0,
        thermal_sensitivity="none",
        needs_thermal_relief=False,
    )


def _is_heat_source(ref_prefix: str, value_lower: str, footprint_lower: str, fp) -> bool:
    """Determine if component is a heat source."""
    # LDO/Regulator by value
    regulator_keywords = [
        "ldo",
        "reg",
        "7805",
        "7812",
        "7833",
        "1117",
        "ams1117",
        "lm317",
        "lm1117",
        "ap2112",
        "mic5504",
        "tps7",
        "lt1",
        "ld1117",
    ]
    if any(kw in value_lower for kw in regulator_keywords):
        return True

    # MOSFET by reference prefix
    if ref_prefix in ("Q", "T") and any(
        kw in value_lower for kw in ["mosfet", "fet", "irf", "ao", "si"]
    ):
        return True

    # Power MOSFET by footprint (DPAK, D2PAK, TO-220, TO-252, TO-263)
    power_footprints = ["dpak", "d2pak", "to-220", "to-252", "to-263", "to220", "sot223"]
    if any(pf in footprint_lower for pf in power_footprints):
        return True

    # Power resistor: low value (<= 10 ohm) or high wattage
    if ref_prefix == "R":
        resistance = _parse_resistance(value_lower)
        if resistance is not None and resistance <= 10.0:
            return True
        # Check for power rating in footprint name
        if any(kw in footprint_lower for kw in ["2512", "2010", "1206", "1210"]):
            # Larger resistors may be power resistors
            if resistance is not None and resistance <= 100.0:
                return True

    # Diodes in power path
    if ref_prefix == "D":
        power_diode_keywords = ["schottky", "ss", "1n5", "mbr", "b5", "b3"]
        if any(kw in value_lower for kw in power_diode_keywords):
            return True
        # Large footprint diodes
        if any(pf in footprint_lower for pf in power_footprints):
            return True

    # Inductors in power path (typically larger values)
    if ref_prefix == "L":
        # Power inductors often have uH values and specific footprints
        if any(pf in footprint_lower for pf in ["shielded", "power", "smd-"]):
            return True

    return False


def _is_heat_sensitive(ref_prefix: str, value_lower: str, footprint_lower: str) -> bool:
    """Determine if component is heat sensitive."""
    # Crystals (Y prefix)
    if ref_prefix == "Y":
        return True

    # Crystal oscillators in value
    crystal_keywords = ["crystal", "xtal", "mhz", "khz", "32.768"]
    if any(kw in value_lower for kw in crystal_keywords):
        return True

    # Precision voltage references
    ref_keywords = ["ref", "lm4040", "lm385", "tl431", "adref", "lt1009", "max6"]
    if ref_prefix == "U" and any(kw in value_lower for kw in ref_keywords):
        return True

    # Temperature sensors
    temp_keywords = ["tmp", "lm35", "ds18", "ntc", "thermistor", "ptc", "temp"]
    if any(kw in value_lower for kw in temp_keywords):
        return True

    # Precision ADCs/DACs
    precision_keywords = ["ads1", "ad7", "mcp32", "ltc2", "max114", "ads8"]
    if ref_prefix == "U" and any(kw in value_lower for kw in precision_keywords):
        return True

    return False


def _parse_resistance(value: str) -> float | None:
    """Parse resistance value from component value string."""
    value = value.strip().lower()

    # Match patterns like "10", "10r", "10k", "10ohm", "10.5k", etc.
    patterns = [
        (r"^(\d+\.?\d*)\s*m\s*ohm", 0.001),  # milliohm
        (r"^(\d+\.?\d*)\s*ohm", 1.0),  # ohm
        (r"^(\d+\.?\d*)\s*r\b", 1.0),  # R notation
        (r"^(\d+\.?\d*)\s*k", 1000.0),  # kilohm
        (r"^(\d+\.?\d*)\s*meg", 1000000.0),  # megohm
        (r"^(\d+\.?\d*)$", 1.0),  # bare number (assume ohms for low values)
        (r"^r(\d+)", 1.0),  # R10 format
    ]

    for pattern, multiplier in patterns:
        match = re.match(pattern, value)
        if match:
            try:
                return float(match.group(1)) * multiplier
            except ValueError:
                pass

    return None


def _estimate_power_dissipation(ref_prefix: str, value: str, fp) -> float:
    """Estimate typical power dissipation for a component."""
    value_lower = value.lower()

    # LDOs typically dissipate (Vin - Vout) * Iload
    # Conservative estimate: 0.5-2W for LDOs
    if any(kw in value_lower for kw in ["ldo", "reg", "1117", "lm317"]):
        return 1.0

    # Power resistors
    if ref_prefix == "R":
        resistance = _parse_resistance(value_lower)
        if resistance is not None and resistance <= 1.0:
            return 0.5  # Current sense resistors can dissipate significant power

    # MOSFETs (depends heavily on application)
    if ref_prefix in ("Q", "T"):
        return 1.0

    # Diodes
    if ref_prefix == "D":
        return 0.5

    return 0.1  # Default low power


def _needs_thermal_relief(footprint_lower: str, fp) -> bool:
    """Determine if component needs thermal relief (exposed pad, etc.)."""
    # Components with exposed pads or thermal tabs
    exposed_pad_indicators = [
        "ep",
        "epad",
        "exposed",
        "dpak",
        "d2pak",
        "qfn",
        "dfn",
        "to-252",
        "to-263",
        "sot223",
        "sot-223",
        "powerpad",
    ]
    if any(ind in footprint_lower for ind in exposed_pad_indicators):
        return True

    # Check for thermal pad in pads
    if hasattr(fp, "pads"):
        for pad in fp.pads:
            pad_type = getattr(pad, "pad_type", "")
            if "thermal" in str(pad_type).lower():
                return True
            # Large central pads are often thermal pads
            if hasattr(pad, "size") and hasattr(pad, "number"):
                if pad.number in ("EP", "0", ""):  # Common thermal pad numbers
                    return True

    return False


def detect_thermal_constraints(
    pcb: PCB,
    thermal_props: dict[str, ThermalProperties] | None = None,
    config: ThermalConfig | None = None,
) -> list[ThermalConstraint]:
    """
    Auto-detect thermal placement constraints based on component properties.

    Args:
        pcb: Loaded PCB object
        thermal_props: Pre-computed thermal properties (optional)
        config: Thermal configuration (optional)

    Returns:
        List of ThermalConstraint objects
    """
    config = config or ThermalConfig()

    if thermal_props is None:
        thermal_props = classify_thermal_properties(pcb)

    constraints: list[ThermalConstraint] = []

    # Find heat sources and sensitive components
    heat_sources = [
        ref
        for ref, props in thermal_props.items()
        if props.thermal_class == ThermalClass.HEAT_SOURCE
    ]
    heat_sensitive = [
        ref
        for ref, props in thermal_props.items()
        if props.thermal_class == ThermalClass.HEAT_SENSITIVE
    ]

    # Create min_separation constraints between heat sources and sensitive components
    for source_ref in heat_sources:
        for sensitive_ref in heat_sensitive:
            constraints.append(
                ThermalConstraint(
                    constraint_type="min_separation",
                    parameters={
                        "heat_source": source_ref,
                        "sensitive": sensitive_ref,
                        "min_distance_mm": config.heat_source_separation_mm,
                    },
                )
            )

    # Create edge_preference constraints for heat sources
    for source_ref in heat_sources:
        constraints.append(
            ThermalConstraint(
                constraint_type="edge_preference",
                parameters={
                    "component": source_ref,
                    "edge_distance_max_mm": config.edge_preference_max_mm,
                },
            )
        )

    # Create thermal_zone constraint if multiple power components exist
    if config.enable_thermal_zones and len(heat_sources) > 1:
        # Group heat sources that should be placed together
        constraints.append(
            ThermalConstraint(
                constraint_type="thermal_zone",
                parameters={
                    "components": heat_sources,
                    "zone_type": "power_section",
                },
            )
        )

    return constraints


def get_thermal_summary(
    thermal_props: dict[str, ThermalProperties],
) -> dict[str, list[str]]:
    """
    Get a summary of thermal classifications.

    Returns:
        Dictionary with keys "heat_sources", "heat_sensitive", "neutral"
    """
    summary: dict[str, list[str]] = {
        "heat_sources": [],
        "heat_sensitive": [],
        "neutral": [],
    }

    for ref, props in thermal_props.items():
        if props.thermal_class == ThermalClass.HEAT_SOURCE:
            summary["heat_sources"].append(ref)
        elif props.thermal_class == ThermalClass.HEAT_SENSITIVE:
            summary["heat_sensitive"].append(ref)
        else:
            summary["neutral"].append(ref)

    return summary
