"""Bypass capacitor placement checks.

Bypass capacitors (decoupling capacitors) should be placed as close as
possible to the IC power pins they are decoupling, with short, wide traces.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import (
    Mistake,
    MistakeCategory,
    distance,
    is_bypass_cap,
    is_power_net,
)

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint


# Maximum recommended distance from bypass cap to power pin (mm)
MAX_BYPASS_DISTANCE_MM = 3.0

# Warning threshold (mm) - still functional but not optimal
BYPASS_WARNING_DISTANCE_MM = 5.0


class BypassCapDistanceCheck:
    """Check that bypass capacitors are close to IC power pins.

    Bypass capacitors should be within 3mm of the power pin they are
    decoupling. At greater distances, trace inductance reduces filtering
    effectiveness, especially at high frequencies.
    """

    category = MistakeCategory.BYPASS_CAP

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check bypass capacitor placement.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for poorly placed bypass caps
        """
        mistakes: list[Mistake] = []

        # Find all ICs (components with VCC/VDD pins)
        ics = self._find_ics_with_power_pins(pcb)

        # Find all bypass capacitors
        bypass_caps = self._find_bypass_caps(pcb)

        # For each bypass cap, find its associated IC and check distance
        for cap in bypass_caps:
            cap_power_net = self._get_cap_power_net(cap)
            if not cap_power_net:
                continue

            # Find ICs that share this power net
            for ic in ics:
                ic_power_pads = self._get_power_pads(ic, cap_power_net)
                if not ic_power_pads:
                    continue

                # Check distance to each power pad
                for pad in ic_power_pads:
                    pad_pos = self._get_absolute_pad_position(ic, pad)
                    cap_pos = cap.position
                    dist = distance(cap_pos, pad_pos)

                    if dist > BYPASS_WARNING_DISTANCE_MM:
                        mistakes.append(
                            Mistake(
                                category=MistakeCategory.BYPASS_CAP,
                                severity="warning",
                                title="Bypass capacitor too far from power pin",
                                components=[cap.reference, ic.reference],
                                location=cap_pos,
                                explanation=(
                                    f"{cap.reference} is {dist:.1f}mm from {ic.reference} "
                                    f"pin {pad.number} ({cap_power_net}). Bypass capacitors "
                                    f"should be within {MAX_BYPASS_DISTANCE_MM}mm of the power "
                                    f"pin they're decoupling. At {dist:.1f}mm, the inductance "
                                    f"of the trace reduces filtering effectiveness."
                                ),
                                fix_suggestion=(
                                    f"Move {cap.reference} to within {MAX_BYPASS_DISTANCE_MM}mm "
                                    f"of {ic.reference} pin {pad.number}, with a short, wide "
                                    f"trace to both {cap_power_net} and GND."
                                ),
                                learn_more_url="docs/mistakes/bypass-cap-placement.md",
                            )
                        )
                    elif dist > MAX_BYPASS_DISTANCE_MM:
                        mistakes.append(
                            Mistake(
                                category=MistakeCategory.BYPASS_CAP,
                                severity="info",
                                title="Bypass capacitor placement could be improved",
                                components=[cap.reference, ic.reference],
                                location=cap_pos,
                                explanation=(
                                    f"{cap.reference} is {dist:.1f}mm from {ic.reference} "
                                    f"pin {pad.number}. Ideally, bypass capacitors should be "
                                    f"within {MAX_BYPASS_DISTANCE_MM}mm for optimal decoupling."
                                ),
                                fix_suggestion=(
                                    f"Consider moving {cap.reference} closer to {ic.reference} "
                                    f"pin {pad.number} if space permits."
                                ),
                                learn_more_url="docs/mistakes/bypass-cap-placement.md",
                            )
                        )

        return mistakes

    def _find_ics_with_power_pins(self, pcb: PCB) -> list[Footprint]:
        """Find ICs that have power pins (VCC, VDD, etc)."""
        ics = []
        for fp in pcb.footprints:
            # Check if any pad is connected to a power net
            for pad in fp.pads:
                if is_power_net(pad.net_name):
                    ics.append(fp)
                    break
        return ics

    def _find_bypass_caps(self, pcb: PCB) -> list[Footprint]:
        """Find components that appear to be bypass capacitors."""
        caps = []
        for fp in pcb.footprints:
            if is_bypass_cap(fp.reference, fp.value):
                caps.append(fp)
        return caps

    def _get_cap_power_net(self, cap: Footprint) -> str | None:
        """Get the power net a capacitor is connected to."""
        for pad in cap.pads:
            if is_power_net(pad.net_name):
                return pad.net_name
        return None

    def _get_power_pads(self, ic: Footprint, power_net: str) -> list:
        """Get pads on an IC connected to a specific power net."""
        return [pad for pad in ic.pads if pad.net_name == power_net]

    def _get_absolute_pad_position(self, fp: Footprint, pad) -> tuple[float, float]:
        """Calculate absolute position of a pad (footprint pos + pad offset)."""
        # TODO: Handle rotation
        return (
            fp.position[0] + pad.position[0],
            fp.position[1] + pad.position[1],
        )
