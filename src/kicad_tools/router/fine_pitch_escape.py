"""
Fine-pitch escape predicates and manufacturer-aware clearance defaults.

Issue #3371 (P_FP1) -- the foundation layer for the fine-pitch escape ladder.
This module exposes the *pure-logic* primitives that downstream phases build
on:

- :func:`geometry_needs_fine_pitch_escape` -- the Q_FP1 recipe-relative
  predicate.  Returns ``True`` when the trace-plus-clearance corridor cannot
  fit between adjacent pads of a fine-pitch package at the current routing
  parameters.  This is the trigger condition for shrinking clearance (or
  switching escape strategy) in the escape-region near the package.

- :func:`get_default_escape_clearance` -- the Q_FP2 manufacturer-aware
  safe-default helper.  Returns a clearance value that is strictly above
  the manufacturer's minimum capability so the per-net-class
  ``escape_clearance`` overrides cannot accidentally violate fab limits.

No router behaviour is changed by this module.  P_FP2 wires the trigger
predicate into a region detector; P_FP3 applies the per-net-class clearance
through :meth:`DesignRules.get_clearance_for_component`; P_FP4 composes the
ladder with auto-layers and auto-pcb-size; P_FP5 adds the consumer test on
softstart rev B.

Issue: https://github.com/rjwalters/kicad-tools/issues/3371
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .mfr_limits import MfrLimits

__all__ = [
    "ESCAPE_CLEARANCE_SAFETY_MARGIN_MM",
    "geometry_needs_fine_pitch_escape",
    "get_default_escape_clearance",
]


# Q_FP2 safety margin (Issue #3371): the per-net-class escape clearance
# default is set to ``mfr.min_clearance + ESCAPE_CLEARANCE_SAFETY_MARGIN_MM``
# so callers never sit exactly on the manufacturer floor.  0.013mm
# (~0.5 mil) is large enough to absorb the rounding error introduced by
# the 0.1mm routing grid quantisation and the C++ pad-segment validator's
# ``epsilon`` tolerance, while small enough that it does not consume the
# headroom buyers pay for at JLCPCB tier-1 (0.127mm + 0.013mm = 0.14mm,
# which is still well under the recipe-side 0.20mm clearance the
# fine-pitch escape replaces in the corridor).
#
# Hardcoded for now.  If a future manufacturer surfaces a tighter epsilon
# tolerance and the 0.013mm margin starts to bite, promote this to an
# ``MfrLimits`` field rather than tuning the constant globally.
ESCAPE_CLEARANCE_SAFETY_MARGIN_MM: float = 0.013


def geometry_needs_fine_pitch_escape(
    trace_width: float,
    clearance: float,
    pin_pitch: float,
    pad_size: float,
) -> bool:
    """Return True when the trace corridor cannot fit between adjacent pads.

    Implements the Q_FP1 *recipe-relative* trigger from Issue #3371: a
    fine-pitch package needs special escape handling iff a 0-degree trace
    (with full per-side clearance) cannot fit through the gap between two
    neighbouring same-row pads at the current routing parameters.

    Geometry (pin-to-pin centre-to-centre = ``pin_pitch``):

        gap_between_pads = pin_pitch - pad_size              # edge-to-edge
        required_corridor = 2 * (trace_width + clearance)    # trace + clearances

        needs_escape  iff  required_corridor > gap_between_pads

    The predicate is *recipe-relative* because the trigger flips with
    routing parameters: the same UCC27211 SOIC-8 footprint (1.27mm
    pitch, 0.30mm pad) does NOT need escape at 0.20mm clearance + 0.15mm
    trace (corridor 0.70mm <= gap 0.97mm) but DOES need escape at
    0.20mm + 0.30mm (corridor 1.00mm > gap 0.97mm).  This is the right
    semantic: the router only pays the escape-pipeline cost when the
    geometry forces it.

    Args:
        trace_width: Trace width in mm at the escape stub.
        clearance: Per-side clearance in mm between trace edge and pad
            edge.  This is the *backbone* / default clearance, not the
            shrunk fine-pitch clearance (which is what the caller will
            apply *if* this predicate fires).
        pin_pitch: Centre-to-centre pin pitch in mm.
        pad_size: Pad dimension along the pitch axis in mm (typically
            the pad width for a horizontal SOIC row; the *short* axis
            of an elongated leaded pad).

    Returns:
        ``True`` when the corridor is infeasible at these parameters
        (escape pipeline should engage); ``False`` when a trace fits
        through cleanly.

    Example -- UCC27211 SOIC-8 at JLCPCB tier-1 recipe (0.30mm trace,
    0.20mm clearance, 1.27mm pitch, 0.30mm pad):

        >>> geometry_needs_fine_pitch_escape(0.30, 0.20, 1.27, 0.30)
        True

    Example -- same SOIC-8 at relaxed clearance (0.30 trace + 0.15
    clearance fits in the 0.97mm gap):

        >>> geometry_needs_fine_pitch_escape(0.30, 0.15, 1.27, 0.30)
        False

    Example -- 2.54mm-pitch header (DIP-style) is never corridor
    constrained for typical recipes:

        >>> geometry_needs_fine_pitch_escape(0.30, 0.20, 2.54, 0.50)
        False
    """
    gap = pin_pitch - pad_size
    required = 2.0 * (trace_width + clearance)
    return required > gap


def get_default_escape_clearance(mfr_limits: "MfrLimits") -> float:
    """Return the manufacturer-aware safe default for escape clearance.

    Implements the Q_FP2 decision from Issue #3371: the default
    per-net-class escape clearance is the manufacturer's minimum
    clearance plus :data:`ESCAPE_CLEARANCE_SAFETY_MARGIN_MM` so callers
    never sit exactly on the fab floor.  Per-net-class overrides via
    :attr:`kicad_tools.router.rules.NetClassRouting.escape_clearance`
    are still allowed; this helper just supplies the *fallback* value
    when the recipe does not specify one.

    Concrete values (from :mod:`kicad_tools.router.mfr_limits`):

    - jlcpcb / jlcpcb-tier1 / pcbway: ``0.127 + 0.013 = 0.140`` mm
    - oshpark:                        ``0.152 + 0.013 = 0.165`` mm

    Args:
        mfr_limits: Manufacturer capability profile.  The only field
            consumed is :attr:`MfrLimits.min_clearance`.

    Returns:
        Safe default escape clearance in mm.

    Note:
        The safety margin is intentionally small.  Callers that want a
        more generous floor (e.g. for sub-tier-1 manufacturers with
        wider lot-to-lot variation) should set
        :attr:`NetClassRouting.escape_clearance` explicitly rather than
        tuning the global constant -- the constant is calibrated for
        tier-1 fabs where 0.013mm covers grid quantisation + validator
        epsilon.
    """
    return mfr_limits.min_clearance + ESCAPE_CLEARANCE_SAFETY_MARGIN_MM
