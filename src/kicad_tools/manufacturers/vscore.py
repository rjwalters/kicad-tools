"""Per-fab copper clearance to a V-score line (Issue #6177).

``kct panel --cut vcut`` warns about copper near a score line and flanks each
score with a pour keepout (Issue #6164).  How wide that clearance must be is a
fab capability, so it lives in the manufacturer profiles as
:attr:`DesignRules.min_copper_to_vscore_mm
<kicad_tools.manufacturers.base.DesignRules.min_copper_to_vscore_mm>`, with
the fab's published page cited in the profile YAML.

A fab that publishes no V-score figure leaves the field unset.  For those
:func:`vscore_clearance_for` returns an explicit, conservative default and
flags it as unsourced so the CLI can say so instead of presenting a guess as
the fab's rule.
"""

from __future__ import annotations

from typing import NamedTuple

UNSOURCED_VSCORE_CLEARANCE_MM = 0.5
"""Fallback for fabs that publish no copper-to-V-score figure.

Not a fab number: it is the top of the 0.3--0.5 mm range fabs commonly quote,
chosen so an unsourced profile errs toward pulling copper back.  The
fallback is also never below the profile's own routed-edge
``min_copper_to_edge_mm`` (a score line never needs *less* room than a
milled edge).
"""


class VScoreClearance(NamedTuple):
    """The copper-to-score clearance a fab profile resolves to.

    Attributes:
        mm: Required clearance in mm.
        mfr: Canonical manufacturer id the value came from.
        sourced: ``True`` when the profile carries a published figure;
            ``False`` when :data:`UNSOURCED_VSCORE_CLEARANCE_MM` (or the
            profile's routed-edge minimum) was substituted.
    """

    mm: float
    mfr: str
    sourced: bool
    supported: bool | None = None
    """``False`` when the fab's docs say it offers no V-scoring (Issue
    #6205); ``None`` when unknown."""


def vscore_clearance_for(
    manufacturer: str,
    layers: int = 2,
    copper_oz: float = 1.0,
) -> VScoreClearance:
    """Resolve the copper-to-V-score clearance for *manufacturer*.

    Args:
        manufacturer: Profile id or alias accepted by
            :func:`kicad_tools.manufacturers.get_profile`.
        layers: Copper layer count used to pick the stackup's design rules.
        copper_oz: Outer copper weight used to pick the stackup's rules.

    Raises:
        ValueError: If *manufacturer* is not a known profile.
    """
    from kicad_tools.manufacturers import get_profile

    profile = get_profile(manufacturer)
    rules = profile.get_design_rules(layers=layers, copper_oz=copper_oz)
    published = rules.min_copper_to_vscore_mm
    if published is not None:
        return VScoreClearance(float(published), profile.id, True, profile.supports_vscore)
    fallback = max(UNSOURCED_VSCORE_CLEARANCE_MM, float(rules.min_copper_to_edge_mm))
    return VScoreClearance(fallback, profile.id, False, profile.supports_vscore)


__all__ = ["UNSOURCED_VSCORE_CLEARANCE_MM", "VScoreClearance", "vscore_clearance_for"]
