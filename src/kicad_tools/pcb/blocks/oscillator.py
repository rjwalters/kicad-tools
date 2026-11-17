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

        # KiCad 10 pads: 1 EN bottom-left, 2 GND bottom-right, 3 OUT top-right,
        # 4 VDD top-left.
        osc_pads = get_footprint_pads(osc_footprint)
        cap_pads = get_footprint_pads(cap_footprint)

        # Place oscillator
        self.add_component(osc_ref, osc_footprint, 0, 0, pads=osc_pads)

        # Decoupling cap stands left of the package, pad 1 level with VDD (pin 4)
        # and pad 2 one cap-length below it.  Turned 270 degrees so pad 1 is on top.
        vdd_pos = Point(osc_pads["4"][0], osc_pads["4"][1])
        gnd_pos = Point(osc_pads["2"][0], osc_pads["2"][1])
        cap_span = abs(cap_pads["2"][0] - cap_pads["1"][0])
        cap_x = vdd_pos.x - 2.5
        self.add_component(
            cap_ref,
            cap_footprint,
            cap_x,
            vdd_pos.y + cap_span / 2,
            rotation=270,
            pads=cap_pads,
        )
        cap = self.components[cap_ref]

        # VDD runs straight out along its pad's row.  GND drops below the
        # package (clear of EN, pin 1), then runs left and up into the cap, so
        # neither rail crosses OUT, EN or the other rail.
        gnd_row = max(y for _, y in osc_pads.values()) + 1.5
        self.add_trace(vdd_pos, cap.pad_position("1"), width=0.3, net="VDD")
        gnd_run = [
            gnd_pos,
            Point(gnd_pos.x, gnd_row),
            Point(cap_x, gnd_row),
            cap.pad_position("2"),
        ]
        for a_pt, b_pt in zip(gnd_run, gnd_run[1:], strict=False):
            self.add_trace(a_pt, b_pt, width=0.3, net="GND")

        # External ports sit on block copper so inter-block routing has an anchor.
        cap_vdd, cap_gnd = cap.pad_position("1"), cap.pad_position("2")
        self.add_port("VDD", cap_vdd.x, cap_vdd.y, direction="power", internal_pad=f"{cap_ref}.1")
        self.add_port("GND", cap_gnd.x, cap_gnd.y, direction="power", internal_pad=f"{cap_ref}.2")
        out, en = osc_pads["3"], osc_pads["1"]
        self.add_port("OUT", out[0], out[1], direction="out", internal_pad=f"{osc_ref}.3")
        self.add_port("EN", en[0], en[1], direction="in", internal_pad=f"{osc_ref}.1")


__all__ = ["OscillatorBlock"]
