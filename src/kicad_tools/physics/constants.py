"""Physical constants and material properties for electromagnetic calculations.

This module provides:
- Physical constants (speed of light, etc.)
- Dielectric material properties (FR4, Rogers, etc.)
- Copper properties at various weights
"""

from __future__ import annotations

from dataclasses import dataclass

# Physical constants
SPEED_OF_LIGHT = 299792458  # m/s
VACUUM_PERMITTIVITY = 8.854187817e-12  # F/m
VACUUM_PERMEABILITY = 1.2566370614e-6  # H/m

# Copper conductivity
COPPER_CONDUCTIVITY = 5.8e7  # S/m at 20C


@dataclass(frozen=True)
class CopperWeight:
    """Copper foil specification by weight."""

    oz: float  # Weight in oz/ft^2
    thickness_um: float  # Thickness in micrometers
    thickness_mm: float  # Thickness in millimeters

    @classmethod
    def from_oz(cls, oz: float) -> CopperWeight:
        """Create from weight in oz/ft^2."""
        # 1 oz/ft^2 = 35 um (approximately)
        thickness_um = oz * 35.0
        return cls(oz=oz, thickness_um=thickness_um, thickness_mm=thickness_um / 1000)


# Standard copper weights
COPPER_HALF_OZ = CopperWeight.from_oz(0.5)  # 17.5 um
COPPER_1OZ = CopperWeight.from_oz(1.0)  # 35 um
COPPER_2OZ = CopperWeight.from_oz(2.0)  # 70 um


@dataclass(frozen=True)
class DielectricMaterial:
    """Dielectric material properties."""

    name: str
    epsilon_r: float  # Relative permittivity (dielectric constant)
    loss_tangent: float  # tan(delta) at 1 GHz
    description: str = ""

    @property
    def epsilon_eff_approx(self) -> float:
        """Approximate effective epsilon for microstrip (rough estimate)."""
        # For microstrip, epsilon_eff is typically between 1 and epsilon_r
        # This is a very rough approximation; actual value depends on geometry
        return (self.epsilon_r + 1) / 2


# Common PCB dielectric materials
FR4_STANDARD = DielectricMaterial(
    name="FR4",
    epsilon_r=4.5,
    loss_tangent=0.02,
    description="Standard FR4 glass-reinforced epoxy laminate",
)

FR4_HIGH_TG = DielectricMaterial(
    name="FR4 High-Tg",
    epsilon_r=4.4,
    loss_tangent=0.018,
    description="High glass transition temperature FR4",
)

ROGERS_4350B = DielectricMaterial(
    name="Rogers RO4350B",
    epsilon_r=3.48,
    loss_tangent=0.0037,
    description="High-frequency laminate with low loss",
)

ROGERS_4003C = DielectricMaterial(
    name="Rogers RO4003C",
    epsilon_r=3.55,
    loss_tangent=0.0027,
    description="Woven glass reinforced hydrocarbon/ceramic",
)

ISOLA_370HR = DielectricMaterial(
    name="Isola 370HR",
    epsilon_r=4.0,
    loss_tangent=0.015,
    description="High-performance FR4 alternative",
)

# Material database by name (case-insensitive lookup)
MATERIALS: dict[str, DielectricMaterial] = {
    "fr4": FR4_STANDARD,
    "fr-4": FR4_STANDARD,
    "fr4 high-tg": FR4_HIGH_TG,
    "fr4_high_tg": FR4_HIGH_TG,
    "rogers 4350b": ROGERS_4350B,
    "ro4350b": ROGERS_4350B,
    "rogers 4003c": ROGERS_4003C,
    "ro4003c": ROGERS_4003C,
    "isola 370hr": ISOLA_370HR,
    "370hr": ISOLA_370HR,
}


def get_material(name: str) -> DielectricMaterial | None:
    """Look up material by name (case-insensitive).

    Args:
        name: Material name (e.g., "FR4", "Rogers 4350B")

    Returns:
        DielectricMaterial if found, None otherwise
    """
    return MATERIALS.get(name.lower())


def get_material_or_default(
    name: str | None, default: DielectricMaterial = FR4_STANDARD
) -> DielectricMaterial:
    """Look up material by name, returning default if not found.

    Args:
        name: Material name (e.g., "FR4", "Rogers 4350B")
        default: Default material if not found

    Returns:
        DielectricMaterial
    """
    if not name:
        return default
    return MATERIALS.get(name.lower(), default)


def copper_thickness_from_oz(oz: float) -> float:
    """Convert copper weight (oz/ft^2) to thickness (mm).

    Args:
        oz: Copper weight in oz/ft^2

    Returns:
        Thickness in mm
    """
    return oz * 0.035  # 1 oz = 35 um = 0.035 mm
