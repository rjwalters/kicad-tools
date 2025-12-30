#!/usr/bin/env python3
"""
Generate a KiCad PCB for a USB joystick controller.

This script creates a PCB file with:
- 32-pin QFP microcontroller (ATmega32U4-style)
- USB Type-C connector
- 2-axis analog joystick (potentiometers)
- 4 tactile buttons
- Crystal oscillator
- Decoupling capacitors
- ESD protection diodes

This demonstrates routing of:
- USB differential pairs (D+, D-)
- Analog signals (joystick axes)
- Digital I/O (buttons)
- Power distribution

Usage:
    python generate_pcb.py [output_file]
"""

import sys
import uuid
from pathlib import Path


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# Board dimensions (mm) - larger board for easier routing
BOARD_WIDTH = 80.0
BOARD_HEIGHT = 60.0
BOARD_ORIGIN_X = 100.0
BOARD_ORIGIN_Y = 100.0

# Net definitions
NETS = {
    "": 0,
    # Power
    "VBUS": 1,
    "VCC": 2,
    "GND": 3,
    # USB
    "USB_D+": 4,
    "USB_D-": 5,
    "USB_CC1": 6,
    "USB_CC2": 7,
    # Analog joystick
    "JOY_X": 8,
    "JOY_Y": 9,
    "JOY_BTN": 10,
    # Buttons
    "BTN1": 11,
    "BTN2": 12,
    "BTN3": 13,
    "BTN4": 14,
    # Crystal
    "XTAL1": 15,
    "XTAL2": 16,
}


def generate_header() -> str:
    """Generate the PCB file header."""
    return f"""(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (generator_version "8.0")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user)
    (39 "F.Mask" user)
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )"""


def generate_nets() -> str:
    """Generate net definitions."""
    lines = ["  (net 0 \"\")"]
    for name, num in NETS.items():
        if num > 0:
            lines.append(f'  (net {num} "{name}")')
    return "\n".join(lines)


def generate_board_outline() -> str:
    """Generate the board outline (Edge.Cuts)."""
    x1 = BOARD_ORIGIN_X
    y1 = BOARD_ORIGIN_Y
    x2 = BOARD_ORIGIN_X + BOARD_WIDTH
    y2 = BOARD_ORIGIN_Y + BOARD_HEIGHT
    return f"""  (gr_rect (start {x1} {y1}) (end {x2} {y2})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "{generate_uuid()}")
  )"""


def generate_mcu() -> str:
    """Generate 32-pin QFP microcontroller footprint."""
    x = BOARD_ORIGIN_X + 40  # Center of board
    y = BOARD_ORIGIN_Y + 32

    # TQFP-32 7x7mm, 0.8mm pitch
    # 8 pins per side
    pitch = 0.8
    body_half = 3.5  # Distance from center to pin row
    pad_offset = 4.5  # Distance from center to pad center

    # Pin assignments (simplified ATmega32U4-like)
    # Left side (pins 1-8): USB, crystal
    # Bottom (pins 9-16): GPIO, ADC
    # Right (pins 17-24): GPIO, power
    # Top (pins 25-32): GPIO, power

    # Pin assignments - USB signals on TOP side (near USB connector)
    # This makes routing from USB-C to MCU much easier
    pin_nets = {
        # Left side (bottom to top) - Crystal and power
        1: ("GND", 3),
        2: ("XTAL1", 15),
        3: ("XTAL2", 16),
        4: ("VCC", 2),
        5: ("", 0),  # NC
        6: ("", 0),  # NC
        7: ("GND", 3),
        8: ("VCC", 2),
        # Bottom (left to right) - GPIO
        9: ("JOY_X", 8),
        10: ("JOY_Y", 9),
        11: ("JOY_BTN", 10),
        12: ("BTN1", 11),
        13: ("BTN2", 12),
        14: ("BTN3", 13),
        15: ("BTN4", 14),
        16: ("GND", 3),
        # Right side (bottom to top) - Power
        17: ("VCC", 2),
        18: ("", 0),  # NC
        19: ("", 0),  # NC
        20: ("", 0),  # NC
        21: ("", 0),  # NC
        22: ("", 0),  # NC
        23: ("GND", 3),
        24: ("VCC", 2),
        # Top (right to left) - USB signals (closest to USB connector)
        25: ("GND", 3),
        26: ("USB_CC2", 7),
        27: ("USB_CC1", 6),
        28: ("USB_D-", 5),
        29: ("USB_D+", 4),
        30: ("VBUS", 1),
        31: ("", 0),  # NC
        32: ("GND", 3),
    }

    pads = []

    # TQFP-32 pin positions: 8 pins per side, centered at 0
    # Positions: -2.8, -2.0, -1.2, -0.4, 0.4, 1.2, 2.0, 2.8 mm
    def pin_offset(i):
        return (i - 3.5) * pitch  # i=0 -> -2.8, i=7 -> 2.8

    # Left side (pins 1-8, bottom to top)
    for i in range(8):
        pin = i + 1
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        py = pin_offset(i)  # Bottom to top: -2.8 to 2.8
        pads.append(f'    (pad "{pin}" smd rect (at {-pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

    # Bottom (pins 9-16, left to right)
    for i in range(8):
        pin = i + 9
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        px = pin_offset(i)  # Left to right: -2.8 to 2.8
        pads.append(f'    (pad "{pin}" smd rect (at {px:.3f} {pad_offset:.3f}) (size 0.5 1.2) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

    # Right side (pins 17-24, bottom to top)
    for i in range(8):
        pin = i + 17
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        py = pin_offset(i)  # Bottom to top: -2.8 to 2.8
        pads.append(f'    (pad "{pin}" smd rect (at {pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

    # Top (pins 25-32, right to left)
    for i in range(8):
        pin = i + 25
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        px = -pin_offset(i)  # Right to left: 2.8 to -2.8
        pads.append(f'    (pad "{pin}" smd rect (at {px:.3f} {-pad_offset:.3f}) (size 0.5 1.2) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

    pads_str = "\n".join(pads)

    return f"""  (footprint "Package_QFP:TQFP-32_7x7mm_P0.8mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U1" (at 0 -6) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MCU" (at 0 6) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_usb_connector() -> str:
    """Generate USB Type-C connector footprint."""
    x = BOARD_ORIGIN_X + 40  # Center of board
    y = BOARD_ORIGIN_Y + 8   # Near top edge

    # Simplified USB-C with main pins
    # A side and B side pins are mirrored
    pins = [
        # Pin, X offset, Net
        ("A1", -2.75, "GND"),
        ("A4", -1.75, "VBUS"),
        ("A5", -1.0, "USB_CC1"),
        ("A6", -0.25, "USB_D+"),
        ("A7", 0.25, "USB_D-"),
        ("A8", 1.0, ""),  # SBU1, NC
        ("A9", 1.75, "VBUS"),
        ("A12", 2.75, "GND"),
        # B side (directly below A)
        ("B1", 2.75, "GND"),
        ("B4", 1.75, "VBUS"),
        ("B5", 1.0, "USB_CC2"),
        ("B6", 0.25, "USB_D+"),
        ("B7", -0.25, "USB_D-"),
        ("B8", -1.0, ""),  # SBU2, NC
        ("B9", -1.75, "VBUS"),
        ("B12", -2.75, "GND"),
    ]

    pads = []
    for pin, px, net_name in pins:
        net_num = NETS.get(net_name, 0)
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        # A-side at y=0, B-side at y=1.0 (wider spacing for routing)
        py = 0 if pin.startswith("A") else 1.0
        pads.append(f'    (pad "{pin}" smd rect (at {px:.2f} {py:.2f}) (size 0.3 1.0) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

    # Shield/mounting tabs
    pads.append(f'    (pad "S1" thru_hole circle (at -4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))')
    pads.append(f'    (pad "S2" thru_hole circle (at 4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))')

    pads_str = "\n".join(pads)

    return f"""  (footprint "Connector_USB:USB_C_Receptacle_GCT_USB4105"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "USB-C" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_joystick() -> str:
    """Generate analog joystick footprint (2-axis + button)."""
    x = BOARD_ORIGIN_X + 15  # Left side of board
    y = BOARD_ORIGIN_Y + 35

    # Typical analog joystick module pinout
    # 5 pins: GND, VCC, VRx, VRy, SW
    pins = [
        ("1", -4, 0, "GND"),
        ("2", -2, 0, "VCC"),
        ("3", 0, 0, "JOY_X"),
        ("4", 2, 0, "JOY_Y"),
        ("5", 4, 0, "JOY_BTN"),
    ]

    pads = []
    for pin, px, py, net_name in pins:
        net_num = NETS[net_name]
        pads.append(f'    (pad "{pin}" thru_hole circle (at {px} {py}) (size 1.6 1.6) (drill 1.0) (layers "*.Cu" "*.Mask") (net {net_num} "{net_name}"))')

    pads_str = "\n".join(pads)

    return f"""  (footprint "Module:Joystick_Analog"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "JOY1" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "Joystick" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_button(ref: str, pos: tuple, net_name: str) -> str:
    """Generate tactile button footprint."""
    x, y = pos
    net_num = NETS[net_name]

    # 6x6mm tactile switch, 2 pins (other 2 are GND)
    return f"""  (footprint "Button_Switch_SMD:SW_SPST_TL3342"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -3.5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "Button" (at 0 3.5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (pad "1" smd rect (at -3.1 0) (size 1.8 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net {net_num} "{net_name}"))
    (pad "2" smd rect (at 3.1 0) (size 1.8 1.4) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))
  )"""


def generate_crystal() -> str:
    """Generate crystal oscillator footprint."""
    x = BOARD_ORIGIN_X + 55  # Right of MCU
    y = BOARD_ORIGIN_Y + 28

    # HC49 crystal, 2 pins
    return f"""  (footprint "Crystal:Crystal_HC49-U_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "Y1" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "16MHz" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (pad "1" thru_hole circle (at -2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net 15 "XTAL1"))
    (pad "2" thru_hole circle (at 2.44 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu" "*.Mask") (net 16 "XTAL2"))
  )"""


def generate_capacitor(ref: str, pos: tuple, net1: str, net2: str) -> str:
    """Generate 0402 decoupling capacitor."""
    x, y = pos
    net1_num = NETS[net1]
    net2_num = NETS[net2]

    return f"""  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.5 0.5) (thickness 0.1)))
    )
    (fp_text value "100nF" (at 0 1.2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.5 0.5) (thickness 0.1)))
    )
    (pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""


def generate_pcb() -> str:
    """Generate the complete PCB file."""
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
        generate_mcu(),
        generate_usb_connector(),
        generate_joystick(),
        generate_crystal(),
    ]

    # Buttons in a row at the bottom right
    button_y = BOARD_ORIGIN_Y + 52
    button_positions = [
        ("SW1", (BOARD_ORIGIN_X + 20, button_y), "BTN1"),
        ("SW2", (BOARD_ORIGIN_X + 35, button_y), "BTN2"),
        ("SW3", (BOARD_ORIGIN_X + 50, button_y), "BTN3"),
        ("SW4", (BOARD_ORIGIN_X + 65, button_y), "BTN4"),
    ]
    for ref, pos, net in button_positions:
        parts.append(generate_button(ref, pos, net))

    # Decoupling capacitors near MCU
    cap_positions = [
        ("C1", (BOARD_ORIGIN_X + 32, BOARD_ORIGIN_Y + 28), "VCC", "GND"),
        ("C2", (BOARD_ORIGIN_X + 48, BOARD_ORIGIN_Y + 28), "VCC", "GND"),
        ("C3", (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 42), "VCC", "GND"),
        ("C4", (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 15), "VBUS", "GND"),  # USB input cap
    ]
    for ref, pos, net1, net2 in cap_positions:
        parts.append(generate_capacitor(ref, pos, net1, net2))

    parts.append(")")  # Close kicad_pcb

    return "\n".join(parts)


def main():
    """Generate the PCB file."""
    output_file = sys.argv[1] if len(sys.argv) > 1 else "usb_joystick.kicad_pcb"
    output_path = Path(__file__).parent / output_file

    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print(f"  Components:")
    print(f"    - 1 MCU (32-pin QFP)")
    print(f"    - 1 USB-C connector")
    print(f"    - 1 Analog joystick module")
    print(f"    - 4 Tactile buttons")
    print(f"    - 1 Crystal oscillator")
    print(f"    - 4 Decoupling capacitors")
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])}")


if __name__ == "__main__":
    main()
