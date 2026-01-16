"""Thermal pad connection checks.

Exposed thermal pads on ICs must be properly connected to the ground
plane for heat dissipation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory, is_ground_net

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint


class ThermalPadConnectionCheck:
    """Check that thermal pads are properly connected.

    Many ICs have exposed thermal pads (EP, exposed pad) that must be
    soldered to a copper area connected to ground for proper heat
    dissipation. Missing or poor connections cause overheating.
    """

    category = MistakeCategory.THERMAL

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check thermal pad connections.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for disconnected thermal pads
        """
        mistakes: list[Mistake] = []

        for fp in pcb.footprints:
            thermal_pads = self._find_thermal_pads(fp)

            for pad in thermal_pads:
                # Check if thermal pad is connected to ground
                if not pad.net_name:
                    mistakes.append(
                        Mistake(
                            category=MistakeCategory.THERMAL,
                            severity="error",
                            title="Thermal pad not connected",
                            components=[fp.reference],
                            location=fp.position,
                            explanation=(
                                f"{fp.reference} has an exposed thermal pad (pad {pad.number}) "
                                f"that is not connected to any net. Thermal pads must be "
                                f"connected to ground with multiple vias for heat dissipation. "
                                f"Unconnected thermal pads cause device overheating and failure."
                            ),
                            fix_suggestion=(
                                f"Connect the thermal pad on {fp.reference} to ground (GND). "
                                f"Add a copper pour under the pad with 4-9 thermal vias "
                                f"connecting to the ground plane."
                            ),
                            learn_more_url="docs/mistakes/thermal-pad-connection.md",
                        )
                    )
                elif not is_ground_net(pad.net_name):
                    mistakes.append(
                        Mistake(
                            category=MistakeCategory.THERMAL,
                            severity="warning",
                            title="Thermal pad not connected to ground",
                            components=[fp.reference],
                            location=fp.position,
                            explanation=(
                                f"{fp.reference} thermal pad (pad {pad.number}) is connected "
                                f"to {pad.net_name} instead of ground. While some devices "
                                f"may specify different connections, most thermal pads should "
                                f"connect to ground for best heat dissipation."
                            ),
                            fix_suggestion=(
                                f"Verify {fp.reference} datasheet for thermal pad connection "
                                f"requirements. Most devices require ground connection with "
                                f"thermal vias."
                            ),
                            learn_more_url="docs/mistakes/thermal-pad-connection.md",
                        )
                    )
                else:
                    # Check for thermal vias nearby
                    vias_count = self._count_nearby_vias(pcb, fp, pad)
                    if vias_count < 4:
                        mistakes.append(
                            Mistake(
                                category=MistakeCategory.THERMAL,
                                severity="info",
                                title="Thermal pad may need more vias",
                                components=[fp.reference],
                                location=fp.position,
                                explanation=(
                                    f"{fp.reference} thermal pad has only {vias_count} via(s) "
                                    f"nearby. For effective heat transfer to the ground plane, "
                                    f"4-9 vias are typically recommended under the thermal pad."
                                ),
                                fix_suggestion=(
                                    f"Add more thermal vias under {fp.reference}'s thermal pad. "
                                    f"Use 0.3mm drill vias in a grid pattern. Consider via-in-pad "
                                    f"with filling if assembly process allows."
                                ),
                                learn_more_url="docs/mistakes/thermal-pad-connection.md",
                            )
                        )

        return mistakes

    def _find_thermal_pads(self, fp: Footprint) -> list:
        """Find thermal/exposed pads on a footprint.

        Thermal pads are typically:
        - Large SMD pads (larger than regular pads)
        - Often numbered as 'EP', '0', or a high number
        - Located at the center of the footprint
        """
        thermal_pads = []

        if not fp.pads:
            return thermal_pads

        # Calculate average pad size to identify unusually large pads
        sizes = [pad.size[0] * pad.size[1] for pad in fp.pads if pad.type == "smd"]
        if not sizes:
            return thermal_pads

        avg_size = sum(sizes) / len(sizes)

        for pad in fp.pads:
            if pad.type != "smd":
                continue

            pad_size = pad.size[0] * pad.size[1]

            # Check for thermal pad indicators
            is_thermal = (
                # Significantly larger than other pads (> 3x average)
                (pad_size > avg_size * 3)
                # Or named EP (exposed pad)
                or pad.number.upper() in ("EP", "0", "EPAD", "THERMAL")
            )

            if is_thermal:
                thermal_pads.append(pad)

        return thermal_pads

    def _count_nearby_vias(self, pcb: PCB, fp: Footprint, pad) -> int:
        """Count vias near a thermal pad.

        Args:
            pcb: The PCB
            fp: The footprint
            pad: The thermal pad

        Returns:
            Number of vias within the pad area
        """
        # Calculate pad position in board coordinates
        pad_x = fp.position[0] + pad.position[0]
        pad_y = fp.position[1] + pad.position[1]
        pad_w, pad_h = pad.size

        count = 0
        for via in pcb.vias:
            vx, vy = via.position
            # Check if via is within pad bounds (with small margin)
            if abs(vx - pad_x) <= pad_w / 2 + 0.5 and abs(vy - pad_y) <= pad_h / 2 + 0.5:
                count += 1

        return count
