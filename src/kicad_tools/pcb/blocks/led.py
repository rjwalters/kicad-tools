"""
LED blocks.

LED with current-limiting resistor.
"""

from ..placement import get_footprint_pads
from .base import PCBBlock


class LEDBlock(PCBBlock):
    """
    LED with current-limiting resistor.
    """

    def __init__(
        self,
        led_ref: str = "D1",
        res_ref: str = "R1",
        led_footprint: str = "LED_SMD:LED_0603_1608Metric",
        res_footprint: str = "Resistor_SMD:R_0603_1608Metric",
    ):
        super().__init__(name=f"LED_{led_ref}")

        led_pads = {"1": (-0.8, 0), "2": (0.8, 0)}  # 1=cathode, 2=anode
        res_pads = get_footprint_pads(res_footprint)

        # LED and resistor in line.  The LED is turned 180 degrees so its
        # cathode (pad 1) faces the resistor and its anode (pad 2) faces the
        # ANODE port: at rotation 0 the cathode-to-resistor trace ran
        # straight through the anode pad, a short (Issue #7022 review).
        self.add_component(led_ref, led_footprint, 0, 0, rotation=180, pads=led_pads)
        self.add_component(res_ref, res_footprint, 3.0, 0, pads=res_pads)

        # LED cathode to resistor
        led = self.components[led_ref]
        res = self.components[res_ref]
        self.add_trace(led.pad_position("1"), res.pad_position("1"), width=0.25, net="LED_MID")

        # External ports
        self.add_port("ANODE", -2.5, 0, direction="in", internal_pad=f"{led_ref}.2")
        self.add_port("CATHODE", 5.5, 0, direction="out", internal_pad=f"{res_ref}.2")


__all__ = ["LEDBlock"]
