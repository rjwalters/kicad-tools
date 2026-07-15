"""IPC-2221 ampacity: derive minimum trace width from target current.

This module provides a closed-form inversion of the IPC-2221 empirical
current-carrying-capacity formula, giving the minimum trace width (mm)
needed to carry a target current at a given temperature rise and copper
weight.

Unlike :func:`kicad_tools.physics.TransmissionLine.width_for_impedance`
(a stackup bisection solver), the ampacity width is a direct closed-form
computation and does NOT depend on the board stackup or
:class:`TransmissionLine`.

The IPC-2221 formula relates current to copper cross-sectional area:

    I = k * delta_t_c**0.44 * A**0.725

where

    - I: current in amps
    - delta_t_c: temperature rise above ambient, in degrees Celsius
    - A: copper cross-sectional area in mils**2 (1 mil = 0.001 inch)
    - k = 0.048 for external (outer) copper layers
    - k = 0.024 for internal copper layers (internal traces shed heat
      less effectively, so they need much more copper for the same
      current / temperature rise)

Inverting for the required area given a target current:

    A = (I / (k * delta_t_c**0.44))**(1/0.725)

The area is then converted to a width via the copper thickness (derived
from the copper weight in oz/ft**2).

Example:
    >>> from kicad_tools.physics.ampacity import width_for_current
    >>> # 15 A on a 2 oz external layer at a 10 C rise wants ~6.3 mm.
    >>> round(width_for_current(15, copper_weight_oz=2, delta_t_c=10), 2)
    6.29
"""

from __future__ import annotations

# IPC-2221 empirical constant k, by layer position.
_K_EXTERNAL = 0.048
_K_INTERNAL = 0.024

# IPC-2221 exponents.
_DELTA_T_EXPONENT = 0.44
_AREA_EXPONENT = 0.725

# Unit conversions.
_MILS_PER_OZ = 1.378  # 1 oz/ft**2 copper ~= 1.378 mils (~= 0.035 mm) thick
_MM_PER_MIL = 0.0254  # 1 mil = 0.001 inch = 0.0254 mm

_VALID_LAYERS = ("external", "internal")


def width_for_current(
    current_a: float,
    copper_weight_oz: float,
    delta_t_c: float = 10.0,
    layer: str = "external",
) -> float:
    """Derive the minimum trace width (mm) for a target current via IPC-2221.

    Solves the IPC-2221 current-capacity formula
    ``I = k * delta_t_c**0.44 * A**0.725`` for the copper cross-sectional
    area ``A`` given a target current ``I``, then converts that area to a
    trace width using the copper thickness implied by ``copper_weight_oz``.

    Args:
        current_a: Target current in amps (must be > 0).
        copper_weight_oz: Copper foil weight in oz/ft**2 (must be > 0).
            External-layer nets typically use ``DesignRules.outer_copper_oz``;
            internal-layer nets use ``DesignRules.inner_copper_oz``.
        delta_t_c: Allowed temperature rise above ambient, in degrees
            Celsius (must be > 0). Defaults to ``10.0``, the conservative
            value used by most commercial IPC-2221 calculators.
        layer: ``"external"`` (k = 0.048) or ``"internal"`` (k = 0.024).
            Internal layers shed heat less effectively and therefore need
            a wider trace for the same current / temperature rise.

    Returns:
        Minimum trace width in millimeters.

    Raises:
        ValueError: If ``current_a``, ``copper_weight_oz``, or
            ``delta_t_c`` is non-positive, or if ``layer`` is not one of
            ``{"external", "internal"}``.
    """
    if current_a <= 0:
        raise ValueError(f"current_a must be positive, got {current_a}")
    if copper_weight_oz <= 0:
        raise ValueError(f"copper_weight_oz must be positive, got {copper_weight_oz}")
    if delta_t_c <= 0:
        raise ValueError(f"delta_t_c must be positive, got {delta_t_c}")
    if layer not in _VALID_LAYERS:
        raise ValueError(f"layer must be one of {_VALID_LAYERS}, got {layer!r}")

    k = _K_EXTERNAL if layer == "external" else _K_INTERNAL

    # Invert IPC-2221 for the required cross-sectional area (mils**2).
    area_mils2 = (current_a / (k * delta_t_c**_DELTA_T_EXPONENT)) ** (1.0 / _AREA_EXPONENT)

    # Copper thickness for this weight, in mils.
    thickness_mils = copper_weight_oz * _MILS_PER_OZ

    # width = area / thickness, converted mils -> mm.
    width_mils = area_mils2 / thickness_mils
    return float(width_mils * _MM_PER_MIL)
