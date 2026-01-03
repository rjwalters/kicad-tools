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

        # SOT-23-5 pinout (typical LDO like AP2204):
        # Pin 1: VIN (top left)
        # Pin 2: GND (top center)
        # Pin 3: EN (top right)
        # Pin 4: NC or BYPASS (bottom right)
        # Pin 5: VOUT (bottom left)

        # Place LDO at center
        self.ldo = self.add_component(ldo_ref, ldo_footprint, 0, 0, pads=ldo_pads)

        # Place input cap to the left
        self.add_component(input_cap, cap_footprint, -3.5, 0, rotation=90, pads=cap_pads)

        # Place output caps to the right
        for i, cap_ref in enumerate(output_caps):
            self.add_component(cap_ref, cap_footprint, 3.5 + i * 2.5, 0, rotation=90, pads=cap_pads)

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

        # GND connections
        gnd_pos = Point(ldo_pads["2"][0], ldo_pads["2"][1])
        cin_pad2 = cin.pad_position("2")
        self.add_trace(gnd_pos, cin_pad2, width=trace_width, net="GND")
        for cap_ref in output_caps:
            cout = self.components[cap_ref]
            cout_pad2 = cout.pad_position("2")
            self.add_trace(gnd_pos, cout_pad2, width=trace_width, net="GND")

        # External ports
        left_edge = -5.5
        right_edge = 3.5 + len(output_caps) * 2.5 + 1.5

        self.add_port("VIN", left_edge, 0, direction="power")
        self.add_port("VOUT", right_edge, 0, direction="power")
        self.add_port("GND", 0, 3, direction="power")
        self.add_port("EN", 2, -3, direction="in", internal_pad=f"{ldo_ref}.3")


__all__ = ["LDOBlock"]
