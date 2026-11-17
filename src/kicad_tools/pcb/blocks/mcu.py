"""
MCU block with bypass capacitors.

Places the MCU with bypass caps positioned optimally close to
VDD/VSS pins, with internal routing for power connections.
"""

from ..geometry import Point
from ..placement import get_footprint_pads, kicad_rotate
from .base import PCBBlock


class MCUBlock(PCBBlock):
    """
    MCU with bypass capacitors.

    Places the MCU with bypass caps positioned optimally close to
    VDD/VSS pins, with internal routing for power connections.

    Example for STM32C011 (TSSOP-20):
        - Pin 4 = VDD (left side)
        - Pin 5 = VSS (left side)
        - Bypass caps placed to left of chip
    """

    def __init__(
        self,
        mcu_ref: str = "U1",
        mcu_footprint: str = "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
        bypass_caps: list[str] | None = None,
        cap_footprint: str = "Capacitor_SMD:C_0603_1608Metric",
        vdd_pin: str = "4",
        vss_pin: str = "5",
    ):
        super().__init__(name=f"MCU_{mcu_ref}")

        if bypass_caps is None:
            bypass_caps = ["C1", "C2"]

        # Get footprint pad data
        mcu_pads = get_footprint_pads(mcu_footprint)
        cap_pads = get_footprint_pads(cap_footprint)

        # Place MCU at block center
        self.mcu = self.add_component(mcu_ref, mcu_footprint, 0, 0, pads=mcu_pads)

        # VDD/VSS pad positions
        vdd_pos = Point(mcu_pads[vdd_pin][0], mcu_pads[vdd_pin][1])
        vss_pos = Point(mcu_pads[vss_pin][0], mcu_pads[vss_pin][1])

        # The caps stand beside the power pins, outboard of the package.  Each
        # rail runs out along its own pin's row (a pad's own centre line keeps
        # clear of the neighbouring pins), then the two rails become parallel
        # buses one cap-length apart, so no trace crosses a foreign pad or the
        # other rail:  VDD along its pin row, GND dropping clear of the pad
        # tips onto a second row on the side VSS lies on.
        side = 1 if vdd_pos.x > 0 else -1  # which edge of the package
        row_sign = 1 if vss_pos.y >= vdd_pos.y else -1
        cap_span = abs(cap_pads["2"][0] - cap_pads["1"][0])
        vdd_row = vdd_pos.y
        gnd_row = vdd_pos.y + row_sign * cap_span
        drop_x = vdd_pos.x + side * 1.45  # past the pad tips, short of the caps
        cap_x0 = vdd_pos.x + side * 2.55
        cap_pitch = 1.8

        # Turn each cap so pad 1 faces the VDD row and pad 2 the GND row.
        cap_rotation = 90 if kicad_rotate(Point(cap_pads["1"][0], 0), 90).y * row_sign < 0 else 270

        for i, cap_ref in enumerate(bypass_caps):
            self.add_component(
                cap_ref,
                cap_footprint,
                cap_x0 + side * i * cap_pitch,
                (vdd_row + gnd_row) / 2,
                rotation=cap_rotation,
                pads=cap_pads,
            )

        # Internal routing: VDD pin -> first cap pad 1 -> on to the next cap's pad 1;
        # VSS pin -> out, over to the GND row -> through every cap's pad 2.
        trace_width = 0.3  # Power traces wider
        caps = [self.components[ref] for ref in bypass_caps]
        vdd_run = [vdd_pos, *(c.pad_position("1") for c in caps)]
        gnd_run = [
            vss_pos,
            Point(drop_x, vss_pos.y),
            Point(drop_x, gnd_row),
            *(c.pad_position("2") for c in caps),
        ]
        for run, net in ((vdd_run, "VDD"), (gnd_run, "GND")):
            for a_pt, b_pt in zip(run, run[1:], strict=False):
                self.add_trace(a_pt, b_pt, width=trace_width, net=net)

        # External ports sit on block copper (cap pad centres) so inter-block
        # routing has a real anchor.
        self.add_port(
            "VDD",
            caps[0].pad_position("1").x,
            vdd_row,
            direction="power",
            internal_pad=f"{bypass_caps[0]}.1",
        )
        self.add_port(
            "GND",
            caps[-1].pad_position("2").x,
            gnd_row,
            direction="power",
            internal_pad=f"{bypass_caps[-1]}.2",
        )

        # Signal ports on right side (expose MCU pins)
        # This would be customized based on actual pin usage
        right_edge = 5.0  # Right edge of block

        # Example: expose PA0-PA7 as ports
        for pin_num in range(7, 15):  # Pins 7-14 are on right side for TSSOP-20
            pin_name = str(pin_num)
            if pin_name in mcu_pads:
                pin_pos = Point(mcu_pads[pin_name][0], mcu_pads[pin_name][1])
                self.add_port(
                    f"PIN{pin_num}",
                    right_edge,
                    pin_pos.y,
                    direction="inout",
                    internal_pad=f"{mcu_ref}.{pin_num}",
                )


__all__ = ["MCUBlock"]
