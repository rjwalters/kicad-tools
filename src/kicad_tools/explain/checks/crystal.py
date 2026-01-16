"""Crystal oscillator placement and routing checks.

Crystal oscillators require careful layout:
- Short traces to minimize capacitance and EMI
- Keep away from noisy digital signals
- Proper load capacitor placement
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mistakes import (
    Mistake,
    MistakeCategory,
    distance,
    is_crystal,
    trace_length,
)

if TYPE_CHECKING:
    from ...schema.pcb import PCB, Footprint


# Maximum recommended crystal trace length (mm)
MAX_CRYSTAL_TRACE_LENGTH_MM = 10.0

# Minimum distance from crystal to noisy signals (mm)
MIN_NOISE_DISTANCE_MM = 5.0


class CrystalTraceLengthCheck:
    """Check that crystal traces are short.

    Crystal oscillator traces should be kept short (< 10mm) to minimize:
    - Parasitic capacitance that affects frequency accuracy
    - EMI emissions from the oscillating signal
    - Susceptibility to noise pickup
    """

    category = MistakeCategory.CRYSTAL

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check crystal trace lengths.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for long crystal traces
        """
        mistakes: list[Mistake] = []

        # Find crystal components
        crystals = self._find_crystals(pcb)

        for crystal in crystals:
            # Find nets connected to crystal
            crystal_nets = self._get_crystal_nets(crystal)

            for net_name in crystal_nets:
                # Get all segments on this net
                net_segments = [
                    seg
                    for seg in pcb.segments
                    if pcb.get_net(seg.net_number) and pcb.get_net(seg.net_number).name == net_name
                ]

                if not net_segments:
                    continue

                length = trace_length(net_segments)

                if length > MAX_CRYSTAL_TRACE_LENGTH_MM:
                    mistakes.append(
                        Mistake(
                            category=MistakeCategory.CRYSTAL,
                            severity="warning",
                            title="Crystal trace too long",
                            components=[crystal.reference],
                            location=crystal.position,
                            explanation=(
                                f"Crystal trace {net_name} is {length:.1f}mm long. "
                                f"Crystal oscillator traces should be kept under "
                                f"{MAX_CRYSTAL_TRACE_LENGTH_MM}mm to minimize parasitic "
                                f"capacitance and EMI. Long traces can affect frequency "
                                f"accuracy and increase noise emissions."
                            ),
                            fix_suggestion=(
                                f"Move the microcontroller closer to {crystal.reference}, "
                                f"or reroute {net_name} to shorten the trace. Place load "
                                f"capacitors close to the crystal pins."
                            ),
                            learn_more_url="docs/mistakes/crystal-layout.md",
                        )
                    )

        return mistakes

    def _find_crystals(self, pcb: PCB) -> list[Footprint]:
        """Find crystal oscillator components."""
        return [fp for fp in pcb.footprints if is_crystal(fp.reference, fp.name)]

    def _get_crystal_nets(self, crystal: Footprint) -> list[str]:
        """Get the signal nets connected to a crystal (excluding power/ground)."""
        signal_nets = []
        for pad in crystal.pads:
            if pad.net_name and not self._is_power_or_ground(pad.net_name):
                signal_nets.append(pad.net_name)
        return signal_nets

    def _is_power_or_ground(self, net_name: str) -> bool:
        """Check if net is power or ground."""
        upper = net_name.upper()
        power_ground = ["VCC", "VDD", "GND", "VSS", "3V3", "5V", "GROUND"]
        return any(pg in upper for pg in power_ground)


class CrystalNoiseProximityCheck:
    """Check that crystals are away from noisy signals.

    Crystal oscillators are sensitive to noise and should be placed
    away from:
    - High-speed digital signals
    - Switching power supplies
    - High-current traces
    """

    category = MistakeCategory.CRYSTAL

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check crystal proximity to noisy signals.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for crystals near noise sources
        """
        mistakes: list[Mistake] = []

        # Find crystal components
        crystals = self._find_crystals(pcb)

        # Find potential noise sources (high-speed signals, switching regulators)
        noise_sources = self._find_noise_sources(pcb)

        for crystal in crystals:
            for noise_src, noise_type in noise_sources:
                dist = distance(crystal.position, noise_src.position)

                if dist < MIN_NOISE_DISTANCE_MM:
                    mistakes.append(
                        Mistake(
                            category=MistakeCategory.CRYSTAL,
                            severity="warning",
                            title="Crystal near noise source",
                            components=[crystal.reference, noise_src.reference],
                            location=crystal.position,
                            explanation=(
                                f"{crystal.reference} is only {dist:.1f}mm from "
                                f"{noise_src.reference} ({noise_type}). Crystal oscillators "
                                f"are sensitive to electromagnetic interference. Proximity "
                                f"to noise sources can cause frequency instability or "
                                f"startup failures."
                            ),
                            fix_suggestion=(
                                f"Move {crystal.reference} at least {MIN_NOISE_DISTANCE_MM}mm "
                                f"away from {noise_src.reference}. Consider adding a ground "
                                f"guard ring around the crystal if space is limited."
                            ),
                            learn_more_url="docs/mistakes/crystal-layout.md",
                        )
                    )

        return mistakes

    def _find_crystals(self, pcb: PCB) -> list[Footprint]:
        """Find crystal oscillator components."""
        return [fp for fp in pcb.footprints if is_crystal(fp.reference, fp.name)]

    def _find_noise_sources(self, pcb: PCB) -> list[tuple[Footprint, str]]:
        """Find components that are potential noise sources.

        Returns list of (footprint, noise_type) tuples.
        """
        noise_sources = []

        for fp in pcb.footprints:
            ref_upper = fp.reference.upper()
            name_lower = fp.name.lower()

            # Switching regulators
            if "regulator" in name_lower or "buck" in name_lower or "boost" in name_lower:
                noise_sources.append((fp, "switching regulator"))

            # USB controllers
            elif "usb" in name_lower:
                noise_sources.append((fp, "USB controller"))

            # Motor drivers
            elif "motor" in name_lower or ref_upper.startswith("DRV"):
                noise_sources.append((fp, "motor driver"))

            # High-speed interfaces
            elif "ethernet" in name_lower or "can" in name_lower:
                noise_sources.append((fp, "high-speed interface"))

        return noise_sources
