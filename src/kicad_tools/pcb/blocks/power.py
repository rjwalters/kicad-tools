"""
Power regulation blocks.

LDO regulator with input and output capacitors.
"""

from ..geometry import Point
from ..placement import get_footprint_pads
from .base import PCBBlock


class LDOBlock(PCBBlock):
    """
    LDO regulator with input and output capacitors.

    Standard layout:
        C_in -- LDO -- C_out1 -- C_out2
               |
              GND
    """

    def __init__(
        self,
        ldo_ref: str = "U1",
        ldo_footprint: str = "Package_TO_SOT_SMD:SOT-23-5",
        input_cap: str = "C1",
        output_caps: list[str] | None = None,
        cap_footprint: str = "Capacitor_SMD:C_0805_2012Metric",
    ):
        super().__init__(name=f"LDO_{ldo_ref}")

        if output_caps is None:
            output_caps = ["C2", "C3"]

        ldo_pads = get_footprint_pads(ldo_footprint)
        cap_pads = get_footprint_pads(cap_footprint)

        # SOT-23-5 pinout (typical LDO like AP2204), KiCad library geometry:
        # Pin 1: VIN (left, top)
        # Pin 2: GND (left, middle)
        # Pin 3: EN (left, bottom)
        # Pin 4: NC or BYPASS (right, bottom)
        # Pin 5: VOUT (right, top)

        # Place LDO at center
        self.ldo = self.add_component(ldo_ref, ldo_footprint, 0, 0, pads=ldo_pads)

        # Caps are turned 270 degrees so pad 1 (VIN / VOUT) is on top, facing
        # the regulator's pin 1 / pin 5, and pad 2 (GND) is on the bottom.
        cap_rotation = 270
        gnd_y = 2.5  # GND runs under the package, clear of U1 pins 3 and 4

        # Place input cap to the left
        self.add_component(input_cap, cap_footprint, -3.5, 0, rotation=cap_rotation, pads=cap_pads)

        # Place output caps to the right
        for i, cap_ref in enumerate(output_caps):
            self.add_component(
                cap_ref, cap_footprint, 3.5 + i * 2.5, 0, rotation=cap_rotation, pads=cap_pads
            )

        # Internal routing
        trace_width = 0.4  # Power traces

        # VIN connections
        vin_pos = Point(ldo_pads["1"][0], ldo_pads["1"][1])
        cin = self.components[input_cap]
        cin_pad1 = cin.pad_position("1")
        self.add_trace(vin_pos, cin_pad1, width=trace_width, net="VIN")

        # VOUT connections
        vout_pos = Point(ldo_pads["5"][0], ldo_pads["5"][1])
        for cap_ref in output_caps:
            cout = self.components[cap_ref]
            cout_pad1 = cout.pad_position("1")
            self.add_trace(vout_pos, cout_pad1, width=trace_width, net="VOUT")

        # GND: leave pin 2 to the left between the input cap's pads, drop to
        # gnd_y and run under the package, then up to each output cap's pad 2.
        gnd_pos = Point(ldo_pads["2"][0], ldo_pads["2"][1])
        cin_x = cin.position.x
        gnd_run = [gnd_pos, Point(cin_x, gnd_pos.y), Point(cin_x, gnd_y)]
        if output_caps:
            gnd_run.append(Point(self.components[output_caps[-1]].position.x, gnd_y))
        for a, b in zip(gnd_run, gnd_run[1:], strict=False):
            self.add_trace(a, b, width=trace_width, net="GND")
        for cap_ref in output_caps:
            cout = self.components[cap_ref]
            self.add_trace(
                Point(cout.position.x, gnd_y),
                cout.pad_position("2"),
                width=trace_width,
                net="GND",
            )

        # External ports sit on block copper (a pad centre, or the GND run) so
        # inter-block routing has a real anchor.  A stub out to a free point
        # would be a dangling track end, which the exporter's copper check
        # refuses.
        out_pad1 = self.components[output_caps[-1]].pad_position("1") if output_caps else vout_pos
        self.add_port("VIN", cin_pad1.x, cin_pad1.y, direction="power")
        self.add_port("VOUT", out_pad1.x, out_pad1.y, direction="power")
        self.add_port("GND", 0, gnd_y, direction="power")
        self.add_port("EN", 2, -3, direction="in", internal_pad=f"{ldo_ref}.3")


__all__ = ["LDOBlock"]
