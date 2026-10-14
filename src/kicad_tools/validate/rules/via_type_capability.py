"""Via-type capability DRC rule (Issue #6518).

KiCad decides what a fab must drill from a via's type keyword: ``(via blind
...)``, ``(via buried ...)`` and ``(via micro ...)`` need sequential
lamination or laser drilling, while a keyword-less ``(via ...)`` is a through
via whatever its ``(layers ...)`` pair says (Issue #6485).  Since PR #6514 our
writers emit these keywords, so a board can carry vias the chosen fab cannot
build.  This rule compares every via's keyword against the active
manufacturer profile:

* ``blind`` / ``buried`` need ``DesignRules.blind_buried_vias_supported``.
* ``micro`` needs ``DesignRules.micro_vias_supported``.

Both flags default to ``False``: a profile that has not *sourced* the
capability fails closed (an error), never silently passes.  One error per
offending via, rule_id ``via_type_unsupported``, naming the via (position,
net, layer pair) and the missing capability.

The rule reads only :attr:`kicad_tools.schema.pcb.Via.via_type`; it never
infers a type from the layer pair, because KiCad does not either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..copper_refs import board_origin
from ..copper_refs import via_ref as copper_via_ref
from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Via

#: via keyword -> (DesignRules capability attribute, human-readable name)
VIA_TYPE_CAPABILITIES: dict[str, tuple[str, str]] = {
    "blind": ("blind_buried_vias_supported", "blind/buried vias"),
    "buried": ("blind_buried_vias_supported", "blind/buried vias"),
    "micro": ("micro_vias_supported", "microvias"),
}


class ViaTypeCapabilityRule(DRCRule):
    """Flag blind, buried and micro vias the manufacturer profile cannot build."""

    rule_id = "via_type_unsupported"
    name = "Via Type Capability"
    description = (
        "Detects blind, buried and micro vias on a manufacturer profile that "
        "does not offer the matching drilling process"
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check every typed via against the profile's via-type capabilities.

        Args:
            pcb: The PCB to check.
            design_rules: The active manufacturer's design rules.

        Returns:
            DRCResults with one ``via_type_unsupported`` error per via whose
            type the profile does not support.
        """
        results = DRCResults()
        results.rules_checked = 1
        origin = board_origin(pcb)

        for via in pcb.vias:
            via_type = getattr(via, "via_type", None)
            capability = VIA_TYPE_CAPABILITIES.get(via_type or "")
            if capability is None:
                continue
            attr, label = capability
            if bool(getattr(design_rules, attr, False)):
                continue
            results.add(self._make_violation(via, via_type or "", attr, label, origin))

        return results

    def _make_violation(
        self,
        via: Via,
        via_type: str,
        attr: str,
        label: str,
        origin: tuple[float, float],
    ) -> DRCViolation:
        net_name = via.net_name or ""
        layers = list(via.layers or [])
        span = f"{layers[0]}-{layers[-1]}" if len(layers) >= 2 else "/".join(layers) or "?"
        return DRCViolation(
            rule_id=self.rule_id,
            severity="error",
            message=(
                f"{via_type.capitalize()} via at ({via.position[0]:.3f}, "
                f"{via.position[1]:.3f}) on {span} (net '{net_name}'): the "
                f"current manufacturer profile does not support {label} "
                f"({attr}=False). Replace it with a through via or choose a "
                f"profile that offers {label}."
            ),
            location=(round(via.position[0], 3), round(via.position[1], 3)),
            actual_value=None,
            required_value=None,
            items=(copper_via_ref(via, origin),),
            nets=(net_name,) if net_name else (),
        )
