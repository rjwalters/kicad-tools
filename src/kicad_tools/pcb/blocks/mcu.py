"""
MCU block with bypass capacitors.

Places the MCU with bypass caps positioned optimally close to
VDD/VSS pins, with internal routing for power connections.
"""

from ..geometry import Point
from ..placement import get_footprint_pads
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

        # Place bypass caps close to power pins
        # Caps oriented horizontally, positioned to the left of VDD/VSS
        cap_x = vdd_pos.x - 2.5  # 2.5mm left of MCU edge
        cap_spacing = 2.0

        for i, cap_ref in enumerate(bypass_caps):
            cap_y = (vdd_pos.y + vss_pos.y) / 2 + (i - len(bypass_caps) / 2 + 0.5) * cap_spacing
            self.add_component(
                cap_ref,
                cap_footprint,
                cap_x,
                cap_y,
                rotation=90,  # Rotate for vertical orientation
                pads=cap_pads,
            )

        # Internal routing: VDD to cap pin 1, cap pin 2 to VSS
        trace_width = 0.3  # Power traces wider

        for cap_ref in bypass_caps:
            cap = self.components[cap_ref]
            cap_pad1 = cap.pad_position("1")
            cap_pad2 = cap.pad_position("2")

            # VDD trace: MCU VDD -> cap pin 1
            self.add_trace(vdd_pos, cap_pad1, width=trace_width, net="VDD")

            # VSS trace: cap pin 2 -> MCU VSS
            self.add_trace(cap_pad2, vss_pos, width=trace_width, net="GND")

        # External ports - positioned at block edges
        # Power ports on left side
        self.add_port(
            "VDD", cap_x - 2, vdd_pos.y, direction="power", internal_pad=f"{bypass_caps[0]}.1"
        )
        self.add_port(
            "GND", cap_x - 2, vss_pos.y, direction="power", internal_pad=f"{bypass_caps[-1]}.2"
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
