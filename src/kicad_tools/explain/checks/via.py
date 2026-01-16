"""Via placement checks.

Vias in SMD pads can cause solder wicking during reflow, leading
to unreliable joints.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB


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
        mistakes: list[Mistake] = []

        for fp in pcb.footprints:
            smd_pads = [p for p in fp.pads if p.type == "smd"]

            for pad in smd_pads:
                # Calculate absolute pad position
                pad_x = fp.position[0] + pad.position[0]
                pad_y = fp.position[1] + pad.position[1]
                pad_w, pad_h = pad.size

                # Check for vias within pad bounds
                for via in pcb.vias:
                    vx, vy = via.position
                    if abs(vx - pad_x) <= pad_w / 2 and abs(vy - pad_y) <= pad_h / 2:
                        mistakes.append(
                            Mistake(
                                category=MistakeCategory.VIA,
                                severity="warning",
                                title="Via in SMD pad",
                                components=[fp.reference, f"pad {pad.number}"],
                                location=via.position,
                                explanation=(
                                    f"Via found in SMD pad {pad.number} of {fp.reference}. "
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
