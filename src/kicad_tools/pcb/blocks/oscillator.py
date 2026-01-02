"""
Oscillator blocks.

Crystal oscillator with decoupling capacitor.
"""

from ..geometry import Point
from ..placement import get_footprint_pads
from .base import PCBBlock


class OscillatorBlock(PCBBlock):
    """
    Crystal oscillator with decoupling capacitor.
    """

    def __init__(
        self,
        osc_ref: str = "Y1",
        osc_footprint: str = "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
        cap_ref: str = "C1",
        cap_footprint: str = "Capacitor_SMD:C_0603_1608Metric",
    ):
        super().__init__(name=f"OSC_{osc_ref}")

        # Simplified oscillator pads (4-pin)
        osc_pads = {
            "1": (-1.25, -0.95),  # EN
            "2": (-1.25, 0.95),  # GND
            "3": (1.25, 0.95),  # OUT
            "4": (1.25, -0.95),  # VDD
        }
        cap_pads = get_footprint_pads(cap_footprint)

        # Place oscillator
        self.add_component(osc_ref, osc_footprint, 0, 0, pads=osc_pads)

        # Place decoupling cap near VDD
        self.add_component(cap_ref, cap_footprint, 3.0, -1, rotation=0, pads=cap_pads)

        # Internal routing: VDD to cap
        vdd_pos = Point(osc_pads["4"][0], osc_pads["4"][1])
        cap = self.components[cap_ref]
        self.add_trace(vdd_pos, cap.pad_position("1"), width=0.3, net="VDD")

        gnd_pos = Point(osc_pads["2"][0], osc_pads["2"][1])
        self.add_trace(gnd_pos, cap.pad_position("2"), width=0.3, net="GND")

        # External ports
        self.add_port("VDD", 5, -1, direction="power")
        self.add_port("GND", -3, 1, direction="power")
        self.add_port("OUT", 3, 1, direction="out", internal_pad=f"{osc_ref}.3")
        self.add_port("EN", -3, -1, direction="in", internal_pad=f"{osc_ref}.1")


__all__ = ["OscillatorBlock"]
