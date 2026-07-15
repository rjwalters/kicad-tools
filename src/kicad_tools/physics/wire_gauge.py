"""Wire-gauge (AWG) physical constants for buttress-wire reinforcement.

This module houses the standard American Wire Gauge (AWG) bare-copper
diameter table plus the derived anchor-hole geometry used by the
``kct pcb reinforce`` pass (Unit A of the #4218 buttress-wire
reinforcement design, Part 2 of the #4215 ampacity feature).

A "buttress wire" is a solid-core copper wire soldered along a
high-current trace to carry additional current. To anchor it, the
reinforce pass drills a spaced row of plated through-holes sized so the
wire drops through and solders. This module provides:

* :func:`bare_copper_diameter_mm` -- AWG -> bare-copper diameter (mm).
* :func:`anchor_drill_for_awg` -- drill diameter = wire diameter plus a
  small slip-fit clearance so the wire drops in and solders.
* :func:`anchor_pad_for_drill` -- pad diameter that satisfies the active
  manufacturer's minimum annular-ring floor.
* :func:`wire_ampacity` -- pure ampacity helper (bare/insulated wire of a
  given gauge). Unit E (#4217 follow-up) will import this to credit a
  reinforced trace's effective ampacity; it is *defined and unit-tested*
  here but intentionally NOT wired into any check in this issue.

The AWG diameter table lives here (next to :mod:`physics.ampacity`) so
all "physical wire" constants stay in :mod:`physics`, consistent with
``CopperWeight`` living in :mod:`physics.constants`.

Example:
    >>> from kicad_tools.physics.wire_gauge import (
    ...     bare_copper_diameter_mm,
    ...     anchor_drill_for_awg,
    ...     anchor_pad_for_drill,
    ... )
    >>> round(bare_copper_diameter_mm(16), 3)
    1.291
    >>> drill = anchor_drill_for_awg(16)
    >>> round(drill, 3)
    1.416
    >>> round(anchor_pad_for_drill(drill, min_annular_ring_mm=0.25), 3)
    1.916
"""

from __future__ import annotations

# Standard AWG bare (solid) copper diameters, in millimeters.
#
# AWG diameter follows the geometric progression
# ``d(n) = 0.127 mm * 92 ** ((36 - n) / 39)`` (an AWG step multiplies the
# diameter by ~1.1229). The values below are that formula rounded to
# 3 decimals, matching published standard wire-gauge tables. Only the
# gauges the reinforce pass supports are enumerated; add rows here to
# support more.
_AWG_DIAMETER_MM: dict[int, float] = {
    12: 2.053,
    14: 1.628,
    16: 1.291,
}

# Slip-fit clearance added to the bare-copper diameter to size the anchor
# drill. ~0.1-0.15 mm gives a hand-solderable drop-in fit (the wire seats
# with a small solder fillet rather than an interference press).
DEFAULT_SLIP_FIT_CLEARANCE_MM = 0.125

#: Default wire gauge for the reinforce pass (16 AWG solid core).
DEFAULT_WIRE_GAUGE_AWG = 16

# IPC-2221-style ampacity constants for a free-standing/insulated round
# wire. ``wire_ampacity`` reuses the same empirical form as
# :func:`kicad_tools.physics.ampacity.width_for_current` (external-layer
# constant, since a buttress wire sheds heat on all sides like an outer
# trace) applied to the wire's circular cross-section.
_AMPACITY_K = 0.048  # external / free-air constant
_DELTA_T_EXPONENT = 0.44
_AREA_EXPONENT = 0.725
_MM2_PER_MIL2 = 1.0 / (0.0254**2)  # 1 mil = 0.0254 mm -> mm**2 -> mils**2

_MATH_PI = 3.141592653589793


def supported_gauges() -> tuple[int, ...]:
    """Return the AWG gauges this module knows about, largest wire first."""
    return tuple(sorted(_AWG_DIAMETER_MM, reverse=False))


def bare_copper_diameter_mm(awg: int) -> float:
    """Return the bare (solid) copper diameter for an AWG gauge, in mm.

    Args:
        awg: American Wire Gauge number (e.g. ``16``). Only the gauges in
            :func:`supported_gauges` are known.

    Returns:
        Bare-copper diameter in millimeters.

    Raises:
        ValueError: If ``awg`` is not a supported gauge.
    """
    try:
        return _AWG_DIAMETER_MM[int(awg)]
    except KeyError:
        supported = ", ".join(str(g) for g in supported_gauges())
        raise ValueError(
            f"unsupported wire gauge {awg} AWG; supported gauges: {supported}"
        ) from None


def anchor_drill_for_awg(
    awg: int,
    slip_fit_clearance_mm: float = DEFAULT_SLIP_FIT_CLEARANCE_MM,
) -> float:
    """Return the anchor drill diameter for a wire gauge, in mm.

    The drill is the bare-copper diameter plus a small slip-fit clearance
    so the solid-core wire drops through the plated hole and solders.

    Args:
        awg: American Wire Gauge number.
        slip_fit_clearance_mm: Extra diameter over the bare wire (default
            :data:`DEFAULT_SLIP_FIT_CLEARANCE_MM`). Must be >= 0.

    Returns:
        Anchor drill diameter in millimeters.

    Raises:
        ValueError: If ``awg`` is unsupported or ``slip_fit_clearance_mm``
            is negative.
    """
    if slip_fit_clearance_mm < 0:
        raise ValueError(f"slip_fit_clearance_mm must be >= 0, got {slip_fit_clearance_mm}")
    return bare_copper_diameter_mm(awg) + slip_fit_clearance_mm


def anchor_pad_for_drill(drill_mm: float, min_annular_ring_mm: float) -> float:
    """Return the anchor pad diameter that satisfies the annular-ring floor.

    The annular ring is ``(pad_diameter - drill_diameter) / 2``. To meet
    the manufacturer's minimum, the pad must be at least
    ``drill + 2 * min_annular_ring``. This mirrors the constraint
    ``validate/rules/solder_mask.py::_check_pth_annular_ring`` enforces.

    Args:
        drill_mm: Anchor drill diameter in mm (must be > 0).
        min_annular_ring_mm: Minimum annular ring from the active
            manufacturer profile's ``DesignRules.min_annular_ring_mm``
            (NOT a hardcoded constant). Must be >= 0.

    Returns:
        Anchor pad diameter in millimeters.

    Raises:
        ValueError: If ``drill_mm`` is non-positive or
            ``min_annular_ring_mm`` is negative.
    """
    if drill_mm <= 0:
        raise ValueError(f"drill_mm must be positive, got {drill_mm}")
    if min_annular_ring_mm < 0:
        raise ValueError(f"min_annular_ring_mm must be >= 0, got {min_annular_ring_mm}")
    return drill_mm + 2.0 * min_annular_ring_mm


def wire_ampacity(awg: int, temp_rise_c: float = 10.0) -> float:
    """Return the ampacity (amps) of a bare/insulated wire of this gauge.

    Pure helper. Applies the IPC-2221 external/free-air current-capacity
    form ``I = k * delta_t**0.44 * A**0.725`` to the wire's circular
    copper cross-section, where ``A`` is in mils**2. A free-standing wire
    sheds heat on all sides, so the external constant (``k = 0.048``) is
    used.

    .. note::
        Unit E / #4217 follow-up will import this to credit a reinforced
        trace's effective ampacity (trace copper + buttress wire). It is
        defined and unit-tested here but is intentionally NOT wired into
        #4217's ampacity check in this issue.

    Args:
        awg: American Wire Gauge number.
        temp_rise_c: Allowed temperature rise above ambient, degrees C
            (must be > 0). Defaults to the conservative ``10.0`` used by
            :func:`kicad_tools.physics.ampacity.width_for_current`.

    Returns:
        Ampacity in amps.

    Raises:
        ValueError: If ``awg`` is unsupported or ``temp_rise_c`` <= 0.
    """
    if temp_rise_c <= 0:
        raise ValueError(f"temp_rise_c must be positive, got {temp_rise_c}")

    diameter_mm = bare_copper_diameter_mm(awg)
    radius_mm = diameter_mm / 2.0
    area_mm2 = _MATH_PI * radius_mm * radius_mm
    area_mils2 = area_mm2 * _MM2_PER_MIL2

    return float(_AMPACITY_K * temp_rise_c**_DELTA_T_EXPONENT * area_mils2**_AREA_EXPONENT)
