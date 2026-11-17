"""Via placement checks.

Vias in SMD pads can cause solder wicking during reflow, leading
to unreliable joints.

This check keeps no geometry of its own (issue #7220): it delegates to the
``kct check`` rule :class:`~kicad_tools.validate.rules.via_in_pad.ViaInPadRule`
and maps its ``via_in_pad`` findings to :class:`Mistake`.  The previous copy
added the pad offset to the footprint position without applying the
footprint rotation, which flagged vias next to rotated pads (board 07 C12.1)
and missed vias in them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB


def _no_via_in_pad_rules():
    """Design rules of a fab without via-in-pad: every in-pad via is reported."""
    from ...manufacturers.base import DesignRules

    return DesignRules(  # noqa: MFR001 reason="synthetic fab-agnostic rules: only via_in_pad_supported=False matters; every other limit is zero so no manufacturer limit can leak into the in-pad scan"
        min_trace_width_mm=0.0,
        min_clearance_mm=0.0,
        min_via_drill_mm=0.0,
        min_via_diameter_mm=0.0,
        min_annular_ring_mm=0.0,
        via_in_pad_supported=False,
    )


class ViaInPadCheck:
    """Check for vias in SMD pads without proper filling.

    Vias placed directly in SMD pads can cause solder to wick down
    into the via during reflow, resulting in:
    - Insufficient solder on the pad
    - Cold or unreliable joints
    - Shorts to inner layers

    Via-in-pad is acceptable when properly filled and plated over.
    """

    category = MistakeCategory.VIA

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check for vias in SMD pads.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for via-in-pad issues
        """
        from ...validate.rules.via_in_pad import ViaInPadRule

        results = ViaInPadRule().check(pcb, _no_via_in_pad_rules())
        mistakes: list[Mistake] = []
        for violation in results.violations:
            if violation.rule_id != "via_in_pad":
                continue
            # items == (via_ref, "<REF>-<PAD>")
            pad_label = violation.items[1] if len(violation.items) > 1 else ""
            reference, _, pad_number = pad_label.rpartition("-")
            reference = reference or pad_label
            location = violation.location or (0.0, 0.0)
            mistakes.append(
                Mistake(
                    category=MistakeCategory.VIA,
                    severity="warning",
                    title="Via in SMD pad",
                    components=[reference, f"pad {pad_number}"],
                    location=location,
                    explanation=(
                        f"Via found in SMD pad {pad_number} of {reference}. "
                        f"During reflow soldering, solder can wick down into "
                        f"the via, resulting in insufficient solder on the pad "
                        f"and unreliable connections."
                    ),
                    fix_suggestion=(
                        "Move the via outside the pad area, or specify via-in-pad "
                        "with filled and capped vias in the manufacturing notes. "
                        "Via-in-pad increases cost but is acceptable when properly "
                        "filled."
                    ),
                    learn_more_url="docs/mistakes/via-in-pad.md",
                )
            )
        return mistakes
