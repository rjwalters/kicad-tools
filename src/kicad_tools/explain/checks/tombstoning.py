"""Tombstoning risk detection.

Tombstoning occurs during reflow when one end of a small component
lifts off the board due to unequal solder surface tension.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint


# Small passives prone to tombstoning (pad area in mm²)
MAX_PASSIVE_SIZE_FOR_RISK = 3.0  # ~0603 and smaller


class TombstoningRiskCheck:
    """Check for tombstoning risk on small passives.

    Tombstoning (also called "Manhattan effect") occurs when one end
    of a small SMD component lifts during reflow soldering. This is
    caused by unequal solder paste, pad sizes, or thermal mass on
    each end of the component.

    Risk factors include:
    - Asymmetric pad sizes
    - One pad connected to large copper area
    - Unequal trace widths to each pad
    """

    category = MistakeCategory.MANUFACTURABILITY

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check for tombstoning risk.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for tombstoning risks
        """
        mistakes: list[Mistake] = []

        for fp in pcb.footprints:
            # Only check small 2-pad SMD passives (resistors, capacitors)
            if not self._is_small_passive(fp):
                continue

            if len(fp.pads) != 2:
                continue

            pad1, pad2 = fp.pads[0], fp.pads[1]

            # Check for asymmetric pads
            size1 = pad1.size[0] * pad1.size[1]
            size2 = pad2.size[0] * pad2.size[1]
            size_ratio = max(size1, size2) / min(size1, size2) if min(size1, size2) > 0 else 1

            if size_ratio > 1.2:  # >20% size difference
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.MANUFACTURABILITY,
                        severity="info",
                        title="Asymmetric pads may cause tombstoning",
                        components=[fp.reference],
                        location=fp.position,
                        explanation=(
                            f"{fp.reference} has asymmetric pad sizes "
                            f"({pad1.size[0]:.2f}x{pad1.size[1]:.2f}mm vs "
                            f"{pad2.size[0]:.2f}x{pad2.size[1]:.2f}mm). "
                            f"Unequal pads can cause tombstoning during reflow "
                            f"due to unequal solder surface tension."
                        ),
                        fix_suggestion=(
                            f"Ensure both pads of {fp.reference} are the same size. "
                            f"Check the footprint definition for errors."
                        ),
                        learn_more_url="docs/mistakes/tombstoning.md",
                        measurements={
                            "pad1_width_mm": round(pad1.size[0], 2),
                            "pad1_height_mm": round(pad1.size[1], 2),
                            "pad2_width_mm": round(pad2.size[0], 2),
                            "pad2_height_mm": round(pad2.size[1], 2),
                        },
                    )
                )

        # Thermal imbalance: one terminal solid to its own-layer pour, the
        # other on a trace or thermal relief.  Issue #7220: the geometry is
        # the ``kct check`` ``thermal_asymmetry`` rule's, so both tools report
        # the same passives (the old copy counted any same-net zone on any
        # layer and ignored fill geometry and zone_connect).
        from ...validate.rules.thermal_asymmetry import asymmetric_passives

        for asym in asymmetric_passives(pcb):
            fp = asym.footprint
            solid = asym.solid_pad
            other = asym.other_pad
            solid_layers = sorted({c.layer for c in asym.solid_contacts if c.mode == "solid"})
            zone_net = next(
                (c.zone_net for c in asym.solid_contacts if c.mode == "solid"), solid.net_name
            )
            other_text = (
                "is connected through a thermal relief"
                if asym.other_contacts
                else "reaches copper only through a trace"
            )
            mistakes.append(
                Mistake(
                    category=MistakeCategory.MANUFACTURABILITY,
                    severity="warning",
                    title="Thermal imbalance may cause tombstoning",
                    components=[fp.reference],
                    location=fp.position,
                    explanation=(
                        f"{fp.reference} pad {solid.number} is connected solid to "
                        f"the {zone_net} copper zone on {', '.join(solid_layers)}, "
                        f"while pad {other.number} {other_text}. "
                        f"The thermal mass difference can cause tombstoning: "
                        f"the pad on the zone heats slower, so solder melts "
                        f"unevenly."
                    ),
                    fix_suggestion=(
                        "Add thermal relief to the zone connection, or add "
                        "copper to balance thermal mass on both pads. Consider "
                        "adding thermal spokes or reducing zone connection width."
                    ),
                    learn_more_url="docs/mistakes/tombstoning.md",
                    measurements={"solid_layers": float(len(solid_layers))},
                )
            )

        return mistakes

    def _is_small_passive(self, fp: Footprint) -> bool:
        """Check if footprint is a small passive component."""
        ref_upper = fp.reference.upper()

        # Check reference designator
        if not (ref_upper.startswith("R") or ref_upper.startswith("C")):
            return False

        # Check if SMD
        if fp.attr != "smd":
            return False

        # Check size (small passives have small pads)
        if not fp.pads:
            return False

        total_pad_area = sum(p.size[0] * p.size[1] for p in fp.pads if p.type == "smd")
        return total_pad_area < MAX_PASSIVE_SIZE_FOR_RISK
