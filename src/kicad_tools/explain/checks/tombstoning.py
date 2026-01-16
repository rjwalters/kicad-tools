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
                    )
                )

            # Check for thermal imbalance (one pad on large pour)
            thermal_imbalance = self._check_thermal_imbalance(pcb, fp, pad1, pad2)
            if thermal_imbalance:
                large_pad, zone_net = thermal_imbalance
                mistakes.append(
                    Mistake(
                        category=MistakeCategory.MANUFACTURABILITY,
                        severity="warning",
                        title="Thermal imbalance may cause tombstoning",
                        components=[fp.reference],
                        location=fp.position,
                        explanation=(
                            f"{fp.reference} pad {large_pad.number} is connected to "
                            f"copper zone ({zone_net}), while the other pad is not. "
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

    def _check_thermal_imbalance(
        self,
        pcb: PCB,
        fp: Footprint,
        pad1,
        pad2,
    ) -> tuple | None:
        """Check if one pad has significantly more thermal mass.

        Returns (pad_with_zone, zone_net_name) if imbalance found, else None.
        """
        # Check if either pad is connected to a zone
        pad1_zones = self._count_zone_connections(pcb, fp, pad1)
        pad2_zones = self._count_zone_connections(pcb, fp, pad2)

        if pad1_zones > 0 and pad2_zones == 0:
            return (pad1, self._get_zone_net(pcb, pad1))
        if pad2_zones > 0 and pad1_zones == 0:
            return (pad2, self._get_zone_net(pcb, pad2))

        return None

    def _count_zone_connections(self, pcb: PCB, fp: Footprint, pad) -> int:
        """Count how many zones a pad is connected to."""
        count = 0
        pad_net = pad.net_number

        for zone in pcb.zones:
            if zone.net_number == pad_net:
                count += 1

        return count

    def _get_zone_net(self, pcb: PCB, pad) -> str:
        """Get the net name of a zone connected to this pad."""
        for zone in pcb.zones:
            if zone.net_number == pad.net_number:
                return zone.net_name or f"Net {zone.net_number}"
        return "unknown"
