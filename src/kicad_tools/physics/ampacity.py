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

Pulsed / duty-cycled current (Issue #4980)
------------------------------------------

The IPC-2221 inversion above answers one question: how wide must copper be
to carry a **steady** current at a bounded temperature rise. A branch that
carries a repetitive pulse (switching ripple, an inrush event, an LED
strobe) needs two *different* questions answered, and this module provides
one function for each:

* :func:`rms_current_for_duty_cycle` -- the thermally equivalent RMS
  current of a rectangular pulse train. Joule heating goes as I**2, so the
  steady current that heats the copper equally is the RMS of the waveform,
  not its peak and not its average. Feeding that RMS into
  :func:`width_for_current` gives the *thermal design* width. This is the
  standard quasi-steady treatment and is valid when the pulse period is
  short compared with the trace's thermal time constant; for a pulse long
  enough to heat the copper on its own, the peak should be treated as
  continuous instead (callers that cannot establish which regime applies
  must say so rather than silently averaging -- see
  :meth:`kicad_tools.router.current_paths.CurrentPathSpec.thermal_design_current`).
* :func:`adiabatic_fusing_current` -- the Onderdonk adiabatic fusing
  current: the peak current that would melt the conductor in a pulse of a
  given duration, assuming none of the heat escapes. This is a
  *destruction* floor, not a design target: a trace sized only to survive
  its fusing limit has no margin at all. Because it assumes zero heat
  loss, it is conservative (real traces shed heat, so the true fusing
  current is higher), and it grows without bound as the pulse shortens, so
  it is only meaningful for a *declared*, finite pulse duration.
"""

from __future__ import annotations

import math

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

# --- Onderdonk adiabatic fusing constants (copper) -------------------------
# I = A_cmil * sqrt( log10((T_m - T_a)/(234 + T_a) + 1) / (33 * t) )
#
# ``234`` is the inverse temperature coefficient of resistivity for copper
# (the temperature, in degrees C below zero, at which copper's resistance
# would extrapolate to zero); ``33`` and the log10 form are Onderdonk's
# empirical constants for the adiabatic short-time heating of copper, with
# area expressed in circular mils and time in seconds.
_COPPER_MELTING_C = 1083.0
_COPPER_INVERSE_TEMP_COEFF_C = 234.0
_ONDERDONK_TIME_CONSTANT = 33.0

# 1 circular mil = the area of a circle 1 mil in diameter = pi/4 mils**2,
# so mils**2 -> circular mils multiplies by 4/pi.
_CIRCULAR_MILS_PER_SQUARE_MIL = 4.0 / math.pi


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


def rms_current_for_duty_cycle(
    peak_a: float,
    duty_cycle: float,
    baseline_a: float = 0.0,
) -> float:
    """Thermally equivalent RMS current of a rectangular pulse train.

    The waveform modeled is: ``peak_a`` for ``duty_cycle`` of every period,
    ``baseline_a`` for the remainder. Joule heating is proportional to
    ``I**2``, so the steady current that heats the conductor identically is

        ``I_rms = sqrt(D * peak**2 + (1 - D) * baseline**2)``

    Feed the result into :func:`width_for_current` to get the *thermal*
    width a pulsed branch needs. Note what this deliberately is **not**: it
    is not the peak (which would oversize every switching branch by
    ``1/sqrt(D)``) and not the average (which would undersize it, because
    averaging current instead of power ignores the ``I**2``).

    Validity: the quasi-steady RMS substitution assumes the pulse period is
    short compared with the conductor's thermal time constant, so the
    copper sees the average power rather than each individual pulse. A
    pulse long enough to heat the trace on its own must be treated as
    continuous at its peak instead.

    Args:
        peak_a: Current during the pulse, in amps (must be > 0).
        duty_cycle: Fraction of each period spent at ``peak_a``, in
            ``(0, 1]``.
        baseline_a: Current between pulses, in amps (must be >= 0).
            Defaults to ``0.0`` (the branch is idle between pulses).

    Returns:
        The thermally equivalent RMS current, in amps. Always at least
        ``baseline_a`` and at most ``peak_a``.

    Raises:
        ValueError: If ``peak_a`` is non-positive, ``duty_cycle`` is
            outside ``(0, 1]``, or ``baseline_a`` is negative.
    """
    if peak_a <= 0:
        raise ValueError(f"peak_a must be positive, got {peak_a}")
    if not 0.0 < duty_cycle <= 1.0:
        raise ValueError(f"duty_cycle must be in (0, 1], got {duty_cycle}")
    if baseline_a < 0:
        raise ValueError(f"baseline_a must be non-negative, got {baseline_a}")

    mean_square = duty_cycle * peak_a**2 + (1.0 - duty_cycle) * baseline_a**2
    return float(math.sqrt(mean_square))


def adiabatic_fusing_current(
    width_mm: float,
    copper_weight_oz: float,
    duration_s: float,
    ambient_c: float = 25.0,
) -> float:
    """Onderdonk adiabatic fusing current for a copper trace, in amps.

    Answers "what peak current melts this trace in a pulse this long,
    assuming none of the heat escapes":

        ``I = A_cmil * sqrt(log10((T_m - T_a)/(234 + T_a) + 1) / (33 * t))``

    with ``A_cmil`` the conductor cross-section in circular mils, ``t`` the
    pulse duration in seconds, ``T_m = 1083 C`` (copper's melting point)
    and ``T_a`` the ambient temperature.

    Two properties matter for how a caller should use this:

    * It is a **destruction** limit, not a design limit. Copper that only
      just survives its fusing current has been driven to its melting
      point; any real design wants the *thermal* (RMS / IPC-2221) check to
      govern, with fusing as a hard backstop against an inrush or fault
      pulse.
    * It is **conservative by construction**: assuming zero heat loss
      understates how much current real copper survives, so a pass here is
      trustworthy and a failure is worth investigating rather than
      dismissing.

    Args:
        width_mm: Trace width in mm (must be > 0).
        copper_weight_oz: Copper foil weight in oz/ft**2 (must be > 0);
            sets the conductor thickness.
        duration_s: Pulse duration in seconds (must be > 0). The result
            scales as ``1/sqrt(duration_s)``, so a 4x shorter pulse
            survives 2x the current.
        ambient_c: Starting temperature of the copper in degrees C
            (default ``25.0``). Must be below copper's melting point.

    Returns:
        The fusing current in amps.

    Raises:
        ValueError: If ``width_mm``, ``copper_weight_oz``, or
            ``duration_s`` is non-positive, or if ``ambient_c`` is not
            below copper's melting point.
    """
    if width_mm <= 0:
        raise ValueError(f"width_mm must be positive, got {width_mm}")
    if copper_weight_oz <= 0:
        raise ValueError(f"copper_weight_oz must be positive, got {copper_weight_oz}")
    if duration_s <= 0:
        raise ValueError(f"duration_s must be positive, got {duration_s}")
    if ambient_c >= _COPPER_MELTING_C:
        raise ValueError(
            f"ambient_c must be below copper's melting point "
            f"({_COPPER_MELTING_C} C), got {ambient_c}"
        )
    if ambient_c <= -_COPPER_INVERSE_TEMP_COEFF_C:
        raise ValueError(
            f"ambient_c must be above {-_COPPER_INVERSE_TEMP_COEFF_C} C, got {ambient_c}"
        )

    width_mils = width_mm / _MM_PER_MIL
    thickness_mils = copper_weight_oz * _MILS_PER_OZ
    area_cmil = width_mils * thickness_mils * _CIRCULAR_MILS_PER_SQUARE_MIL

    temp_ratio = (_COPPER_MELTING_C - ambient_c) / (_COPPER_INVERSE_TEMP_COEFF_C + ambient_c)
    numerator = math.log10(temp_ratio + 1.0)
    return float(area_cmil * math.sqrt(numerator / (_ONDERDONK_TIME_CONSTANT * duration_s)))
