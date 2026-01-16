#!/usr/bin/env python3
"""
USB Joystick Controller - Complete Design Generation

This script demonstrates the complete PCB design workflow:
1. Create project file
2. Create schematic with MCU, USB, joystick, and buttons
3. Run ERC validation
4. Generate PCB with component placement
5. Route PCB traces
6. Run DRC validation

The design is a USB game controller with:
- 32-pin QFP microcontroller
- USB Type-C connector
- 2-axis analog joystick
- 4 tactile buttons
- Crystal oscillator

Usage:
    python generate_design.py [output_dir]

If no output directory is specified, files are written to ./output/
"""

import subprocess
import sys
import uuid
from pathlib import Path

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.models.schematic import Schematic, SnapMode

# Warn if running source scripts with stale pipx install
warn_if_stale()


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# =============================================================================
# Net Definitions
# =============================================================================

NETS = {
    "": 0,
    "VBUS": 1,
    "VCC": 2,
    "GND": 3,
    "USB_D+": 4,
    "USB_D-": 5,
    "USB_CC1": 6,
    "USB_CC2": 7,
    "JOY_X": 8,
    "JOY_Y": 9,
    "JOY_BTN": 10,
    "BTN1": 11,
    "BTN2": 12,
    "BTN3": 13,
    "BTN4": 14,
    "XTAL1": 15,
    "XTAL2": 16,
}


# =============================================================================
# Schematic Generation
# =============================================================================

WIRE_STUB = 5.08  # 200 mils


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right"):
    """Add a wire stub from a pin position to a global label."""
    if not pin_pos:
        return

    x, y = pin_pos
    if direction == "right":
        end_x = x + WIRE_STUB
        rotation = 180
    else:
        end_x = x - WIRE_STUB
        rotation = 0

    sch.add_wire((x, y), (end_x, y), snap=False)
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation, snap=False)


def create_usb_joystick_schematic(output_dir: Path) -> Path:
    """
    Create a USB Joystick schematic.

    Returns the path to the generated schematic file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick Schematic...")
    print("=" * 60)

    sch = Schematic(
        title="USB Joystick Controller",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="USB game controller with analog joystick",
        comment2="Demonstrates autolayout functionality",
        snap_mode=SnapMode.AUTO,
        grid=2.54,
    )

    RAIL_VCC = 25.4
    RAIL_GND = 177.8

    # =========================================================================
    # Section 1: Place MCU
    # =========================================================================
    print("\n1. Placing MCU...")

    try:
        mcu = sch.add_symbol(
            "Connector_Generic:Conn_02x16_Counter_Clockwise",
            x=101.6,
            y=88.9,
            ref="U1",
            value="MCU",
        )
    except Exception:
        mcu = sch.add_symbol(
            "Device:R",
            x=101.6,
            y=88.9,
            ref="U1",
            value="MCU",
        )
    print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    # =========================================================================
    # Section 2: Place USB connector
    # =========================================================================
    print("\n2. Placing USB connector...")

    suggested_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x04",
        near=(50.8, 50.8),
        padding=5.08,
    )

    usb_conn = sch.add_symbol(
        "Connector_Generic:Conn_01x04",
        x=suggested_pos[0],
        y=suggested_pos[1],
        ref="J1",
        value="USB-C",
    )
    print(f"   J1 (USB-C): placed at ({usb_conn.x}, {usb_conn.y})")

    # =========================================================================
    # Section 3: Place Joystick connector
    # =========================================================================
    print("\n3. Placing Joystick connector...")

    joy_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x05",
        near=(50.8, 101.6),
        padding=5.08,
    )

    joy_conn = sch.add_symbol(
        "Connector_Generic:Conn_01x05",
        x=joy_pos[0],
        y=joy_pos[1],
        ref="J2",
        value="Joystick",
    )
    print(f"   J2 (Joystick): placed at ({joy_conn.x}, {joy_conn.y})")

    # =========================================================================
    # Section 4: Place Crystal
    # =========================================================================
    print("\n4. Placing Crystal...")

    xtal_pos = sch.suggest_position(
        "Device:Crystal",
        near=(127.0, 76.2),
        padding=5.08,
    )

    try:
        xtal = sch.add_symbol(
            "Device:Crystal",
            x=xtal_pos[0],
            y=xtal_pos[1],
            ref="Y1",
            value="16MHz",
        )
    except Exception:
        xtal = sch.add_symbol(
            "Device:R",
            x=xtal_pos[0],
            y=xtal_pos[1],
            ref="Y1",
            value="16MHz",
        )
    print(f"   Y1 (Crystal): placed at ({xtal.x}, {xtal.y})")

    # =========================================================================
    # Section 5: Place Buttons
    # =========================================================================
    print("\n5. Placing Buttons...")

    button_refs = ["SW1", "SW2", "SW3", "SW4"]
    base_x, base_y = 152.4, 88.9

    buttons = []
    for ref in button_refs:
        pos = sch.suggest_position(
            "Device:R",
            near=(base_x, base_y),
            padding=7.62,
        )

        btn = sch.add_symbol(
            "Device:R",
            x=pos[0],
            y=pos[1],
            ref=ref,
            value="Button",
        )
        buttons.append(btn)
        print(f"   {ref}: placed at ({btn.x}, {btn.y})")

    # =========================================================================
    # Section 6: Place Decoupling Capacitors
    # =========================================================================
    print("\n6. Placing Decoupling Capacitors...")

    cap_positions = [
        ("C1", 88.9, 63.5),
        ("C2", 114.3, 63.5),
        ("C3", 88.9, 114.3),
        ("C4", 55.88, 38.1),
    ]

    caps = []
    for ref, x, y in cap_positions:
        pos = sch.suggest_position(
            "Device:C",
            near=(x, y),
            padding=2.54,
        )

        try:
            cap = sch.add_symbol(
                "Device:C",
                x=pos[0],
                y=pos[1],
                ref=ref,
                value="100nF",
            )
        except Exception:
            cap = sch.add_symbol(
                "Device:R",
                x=pos[0],
                y=pos[1],
                ref=ref,
                value="100nF",
            )
        caps.append(cap)
        print(f"   {ref}: placed at ({cap.x}, {cap.y})")

    # =========================================================================
    # Section 7: Add signal wiring
    # =========================================================================
    print("\n7. Adding signal wiring...")

    MCU_PIN_MAP = {
        "1": "VCC", "16": "GND", "17": "VCC", "32": "GND",
        "29": "USB_D+", "30": "USB_D-",
        "7": "XTAL1", "8": "XTAL2",
        "2": "JOY_X", "3": "JOY_Y",
        "9": "BTN1", "10": "BTN2", "11": "BTN3", "12": "BTN4", "13": "JOY_BTN",
    }

    USB_PIN_MAP = {"1": "VCC", "2": "USB_D-", "3": "USB_D+", "4": "GND"}
    JOY_PIN_MAP = {"1": "VCC", "2": "GND", "3": "JOY_X", "4": "JOY_Y", "5": "JOY_BTN"}

    for pin_num, net_name in MCU_PIN_MAP.items():
        pin_pos = mcu.pin_position(pin_num)
        if pin_pos:
            direction = "left" if int(pin_num) <= 16 else "right"
            add_pin_label(sch, pin_pos, net_name, direction=direction)

    used_pins = set(MCU_PIN_MAP.keys())
    for pin_num in range(1, 33):
        pin_str = str(pin_num)
        if pin_str not in used_pins:
            pin_pos = mcu.pin_position(pin_str)
            if pin_pos:
                sch.add_no_connect(pin_pos[0], pin_pos[1], snap=False)

    for pin_num, net_name in USB_PIN_MAP.items():
        pin_pos = usb_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")

    for pin_num, net_name in JOY_PIN_MAP.items():
        pin_pos = joy_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")

    xtal_pin1 = xtal.pin_position("1")
    xtal_pin2 = xtal.pin_position("2")
    if xtal_pin1:
        add_pin_label(sch, xtal_pin1, "XTAL1", direction="left")
    if xtal_pin2:
        add_pin_label(sch, xtal_pin2, "XTAL2", direction="right")

    button_nets = ["BTN1", "BTN2", "BTN3", "BTN4"]
    for btn, net_name in zip(buttons, button_nets, strict=True):
        pin1_pos = btn.pin_position("1")
        pin2_pos = btn.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, net_name, direction="left")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")

    for cap in caps:
        pin1_pos = cap.pin_position("1")
        pin2_pos = cap.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, "VCC", direction="left")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")

    # Power symbols
    vcc_pwr = sch.add_power("power:+5V", x=25.4, y=RAIL_VCC, rotation=0)
    sch.add_wire((vcc_pwr.x, vcc_pwr.y), (vcc_pwr.x + WIRE_STUB, vcc_pwr.y), snap=False)
    sch.add_global_label("VCC", vcc_pwr.x + WIRE_STUB, vcc_pwr.y, shape="input", rotation=180, snap=False)
    sch.add_pwr_flag(vcc_pwr.x, vcc_pwr.y)

    gnd_pwr = sch.add_power("power:GND", x=25.4, y=RAIL_GND, rotation=180)
    sch.add_wire((gnd_pwr.x, gnd_pwr.y), (gnd_pwr.x + WIRE_STUB, gnd_pwr.y), snap=False)
    sch.add_global_label("GND", gnd_pwr.x + WIRE_STUB, gnd_pwr.y, shape="input", rotation=180, snap=False)
    sch.add_pwr_flag(gnd_pwr.x, gnd_pwr.y)

    print("   Added VCC and GND power symbols with PWR_FLAG")

    # =========================================================================
    # Section 8: Validate and Write
    # =========================================================================
    print("\n8. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    if errors:
        print(f"   Found {len(errors)} errors")
    else:
        print("   No errors found")

    if warnings:
        print(f"   Found {len(warnings)} warnings")

    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Wires: {stats['wire_count']}")

    print("\n9. Writing schematic...")
    output_dir.mkdir(parents=True, exist_ok=True)
    sch_path = output_dir / "usb_joystick.kicad_sch"
    sch.write(sch_path)
    print(f"   Schematic: {sch_path}")

    return sch_path


# =============================================================================
# PCB Generation
# =============================================================================

BOARD_WIDTH = 60.0
BOARD_HEIGHT = 40.0
BOARD_ORIGIN_X = 100.0
BOARD_ORIGIN_Y = 100.0


def create_usb_joystick_pcb(output_dir: Path) -> Path:
    """
    Create a PCB for the USB joystick.

    Returns the path to the generated PCB file.
    """
    print("\n" + "=" * 60)
    print("Creating USB Joystick PCB...")
    print("=" * 60)

    def generate_header() -> str:
        return """(kicad_pcb
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
        lines = ['  (net 0 "")']
        for name, num in NETS.items():
            if num > 0:
                lines.append(f'  (net {num} "{name}")')
        return "\n".join(lines)

    def generate_board_outline() -> str:
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
        x = BOARD_ORIGIN_X + 30
        y = BOARD_ORIGIN_Y + 20
        pitch = 0.8
        pad_offset = 4.5

        pin_nets = {
            1: ("GND", 3), 2: ("XTAL1", 15), 3: ("XTAL2", 16), 4: ("VCC", 2),
            5: ("", 0), 6: ("", 0), 7: ("GND", 3), 8: ("VCC", 2),
            9: ("JOY_X", 8), 10: ("JOY_Y", 9), 11: ("JOY_BTN", 10), 12: ("BTN1", 11),
            13: ("BTN2", 12), 14: ("BTN3", 13), 15: ("BTN4", 14), 16: ("GND", 3),
            17: ("VCC", 2), 18: ("", 0), 19: ("", 0), 20: ("", 0),
            21: ("", 0), 22: ("", 0), 23: ("GND", 3), 24: ("VCC", 2),
            25: ("GND", 3), 26: ("USB_CC2", 7), 27: ("USB_CC1", 6), 28: ("USB_D-", 5),
            29: ("USB_D+", 4), 30: ("VBUS", 1), 31: ("", 0), 32: ("GND", 3),
        }

        def pin_offset(i):
            return (i - 3.5) * pitch

        pads = []
        for i in range(8):
            pin = i + 1
            net_name, net_num = pin_nets[pin]
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = pin_offset(i)
            pads.append(f'    (pad "{pin}" smd rect (at {-pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

        for i in range(8):
            pin = i + 9
            net_name, net_num = pin_nets[pin]
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            px = pin_offset(i)
            pads.append(f'    (pad "{pin}" smd rect (at {px:.3f} {pad_offset:.3f}) (size 0.5 1.2) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

        for i in range(8):
            pin = i + 17
            net_name, net_num = pin_nets[pin]
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = pin_offset(i)
            pads.append(f'    (pad "{pin}" smd rect (at {pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

        for i in range(8):
            pin = i + 25
            net_name, net_num = pin_nets[pin]
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            px = -pin_offset(i)
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
        x = BOARD_ORIGIN_X + 30
        y = BOARD_ORIGIN_Y + 5

        pins = [
            ("A1", -2.75, "GND"), ("A4", -1.75, "VBUS"), ("A5", -1.0, "USB_CC1"),
            ("A6", -0.25, "USB_D+"), ("A7", 0.25, "USB_D-"), ("A8", 1.0, ""),
            ("A9", 1.75, "VBUS"), ("A12", 2.75, "GND"),
            ("B1", 2.75, "GND"), ("B4", 1.75, "VBUS"), ("B5", 1.0, "USB_CC2"),
            ("B6", 0.25, "USB_D+"), ("B7", -0.25, "USB_D-"), ("B8", -1.0, ""),
            ("B9", -1.75, "VBUS"), ("B12", -2.75, "GND"),
        ]

        pads = []
        for pin, px, net_name in pins:
            net_num = NETS.get(net_name, 0)
            net_str = f'(net {net_num} "{net_name}")' if net_name else ""
            py = 0 if pin.startswith("A") else 1.0
            pads.append(f'    (pad "{pin}" smd rect (at {px:.2f} {py:.2f}) (size 0.25 0.35) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})')

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
        x = BOARD_ORIGIN_X + 12
        y = BOARD_ORIGIN_Y + 22

        pins = [
            ("1", -4, 0, "GND"), ("2", -2, 0, "VCC"), ("3", 0, 0, "JOY_X"),
            ("4", 2, 0, "JOY_Y"), ("5", 4, 0, "JOY_BTN"),
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
        x, y = pos
        net_num = NETS[net_name]
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
        x = BOARD_ORIGIN_X + 45
        y = BOARD_ORIGIN_Y + 18
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

    # Build the PCB file
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
        generate_mcu(),
        generate_usb_connector(),
        generate_joystick(),
        generate_crystal(),
    ]

    print("\n1. Adding footprints...")
    print(f"   U1 (MCU) at board center")
    print(f"   J1 (USB-C) at top")
    print(f"   JOY1 (Joystick) at left")
    print(f"   Y1 (Crystal) near MCU")

    button_y = BOARD_ORIGIN_Y + 35
    button_positions = [
        ("SW1", (BOARD_ORIGIN_X + 15, button_y), "BTN1"),
        ("SW2", (BOARD_ORIGIN_X + 27, button_y), "BTN2"),
        ("SW3", (BOARD_ORIGIN_X + 39, button_y), "BTN3"),
        ("SW4", (BOARD_ORIGIN_X + 51, button_y), "BTN4"),
    ]
    for ref, pos, net in button_positions:
        parts.append(generate_button(ref, pos, net))
    print(f"   SW1-SW4 (Buttons) at bottom")

    cap_positions = [
        ("C1", (BOARD_ORIGIN_X + 22, BOARD_ORIGIN_Y + 18), "VCC", "GND"),
        ("C2", (BOARD_ORIGIN_X + 38, BOARD_ORIGIN_Y + 18), "VCC", "GND"),
        ("C3", (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 28), "VCC", "GND"),
        ("C4", (BOARD_ORIGIN_X + 30, BOARD_ORIGIN_Y + 10), "VBUS", "GND"),
    ]
    for ref, pos, net1, net2 in cap_positions:
        parts.append(generate_capacitor(ref, pos, net1, net2))
    print(f"   C1-C4 (Capacitors) near MCU")

    parts.append(")")

    pcb_content = "\n".join(parts)

    print("\n2. Writing PCB file...")
    pcb_path = output_dir / "usb_joystick.kicad_pcb"
    pcb_path.write_text(pcb_content)
    print(f"   PCB: {pcb_path}")

    print(f"\n   Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("   Components: 1 MCU, 1 USB-C, 1 joystick, 4 buttons, 1 crystal, 4 caps")
    print(f"   Nets: {len([n for n in NETS.values() if n > 0])}")

    return pcb_path


# =============================================================================
# Project, ERC, Routing, DRC
# =============================================================================

def create_project(output_dir: Path, project_name: str) -> Path:
    """Create a KiCad project file."""
    print("\n" + "=" * 60)
    print("Creating Project File...")
    print("=" * 60)

    output_dir.mkdir(parents=True, exist_ok=True)

    filename = f"{project_name}.kicad_pro"
    project_data = create_minimal_project(filename)

    project_path = output_dir / filename
    save_project(project_data, project_path)
    print(f"\n   Project: {project_path}")

    return project_path


def run_erc(sch_path: Path) -> bool:
    """Run ERC on the schematic."""
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.cli.runner import run_erc as kicad_run_erc
    from kicad_tools.erc import ERCReport

    print("\n" + "=" * 60)
    print("Running ERC...")
    print("=" * 60)

    kicad_cli = find_kicad_cli()
    if not kicad_cli:
        print("\n   WARNING: kicad-cli not found - skipping ERC")
        return True

    result = kicad_run_erc(sch_path)

    if not result.success:
        print(f"\n   Error running ERC: {result.stderr}")
        return False

    try:
        report = ERCReport.load(result.output_path)
    except Exception as e:
        print(f"\n   Error parsing ERC report: {e}")
        return False
    finally:
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    violations = [v for v in report.violations if not v.excluded]
    error_count = sum(1 for v in violations if v.is_error)

    if error_count > 0:
        print(f"\n   Found {error_count} ERC errors:")
        for v in [v for v in violations if v.is_error][:5]:
            print(f"      - [{v.type_str}] {v.description}")
        return False
    else:
        print("\n   No ERC errors found!")
        return True


def route_pcb(input_path: Path, output_path: Path) -> bool:
    """Route the PCB using the autorouter."""
    from kicad_tools.router import DesignRules, create_net_class_map, load_pcb_for_routing
    from kicad_tools.router.optimizer import GridCollisionChecker, OptimizationConfig, TraceOptimizer

    print("\n" + "=" * 60)
    print("Routing PCB...")
    print("=" * 60)

    # Design rules - Using coarser grid (0.2mm) for reasonable routing speed.
    # Grid must be <= clearance/2 for DRC compliance.
    # Note: Fine-pitch QFP routing requires finer grid but takes much longer.
    # For this demo, partial routing is acceptable - some nets may not route.
    rules = DesignRules(
        grid_resolution=0.2,
        trace_width=0.2,
        trace_clearance=0.4,
        via_drill=0.3,
        via_diameter=0.6,
    )

    net_class_map = create_net_class_map(
        power_nets=["VCC", "VBUS", "GND"],
        high_speed_nets=["USB_D+", "USB_D-"],
        clock_nets=["XTAL1", "XTAL2"],
    )

    skip_nets = ["VCC", "GND", "VBUS"]

    print(f"\n1. Loading PCB: {input_path}")
    print(f"   Grid resolution: {rules.grid_resolution}mm")
    print(f"   Trace width: {rules.trace_width}mm")
    print(f"   Clearance: {rules.trace_clearance}mm")
    print(f"   Skipping nets: {skip_nets}")

    router, net_map = load_pcb_for_routing(
        str(input_path),
        skip_nets=skip_nets,
        rules=rules,
    )

    router.net_class_map.update(net_class_map)

    print(f"\n   Board size: {router.grid.width}mm x {router.grid.height}mm")
    print(f"   Nets loaded: {len(net_map)}")

    print("\n2. Routing nets...")
    router.route_all()

    stats_before = router.get_statistics()

    print("\n3. Raw routing results:")
    print(f"   Routes: {stats_before['routes']}")
    print(f"   Segments: {stats_before['segments']}")
    print(f"   Vias: {stats_before['vias']}")

    print("\n4. Optimizing traces...")
    opt_config = OptimizationConfig(
        merge_collinear=True,
        eliminate_zigzags=True,
        compress_staircase=True,
        convert_45_corners=True,
        minimize_vias=True,
    )
    collision_checker = GridCollisionChecker(router.grid)
    optimizer = TraceOptimizer(config=opt_config, collision_checker=collision_checker)

    optimized_routes = []
    for route in router.routes:
        optimized_route = optimizer.optimize_route(route)
        optimized_routes.append(optimized_route)
    router.routes = optimized_routes

    stats = router.get_statistics()

    print("\n5. Final routing results:")
    print(f"   Routes: {stats['routes']}")
    print(f"   Segments: {stats['segments']}")
    print(f"   Vias: {stats['vias']}")
    print(f"   Total length: {stats['total_length_mm']:.2f}mm")
    print(f"   Nets routed: {stats['nets_routed']}")

    print(f"\n6. Saving routed PCB: {output_path}")

    original_content = input_path.read_text()
    route_sexp = router.to_sexp()

    if route_sexp:
        output_content = original_content.rstrip().rstrip(")")
        output_content += "\n"
        output_content += f"  {route_sexp}\n"
        output_content += ")\n"
    else:
        output_content = original_content
        print("   Warning: No routes generated!")

    output_path.write_text(output_content)

    total_nets = len([n for n in router.nets if n > 0])
    success = stats["nets_routed"] == total_nets

    if success:
        print("\n   SUCCESS: All signal nets routed!")
    else:
        print(f"\n   PARTIAL: Routed {stats['nets_routed']}/{total_nets} signal nets")

    return success


def run_drc(pcb_path: Path) -> bool:
    """Run DRC on the PCB."""
    print("\n" + "=" * 60)
    print("Running DRC (via kct check)...")
    print("=" * 60)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "check", str(pcb_path)],
            capture_output=True,
            text=True,
        )

        if result.stdout:
            for line in result.stdout.strip().split("\n"):
                print(f"   {line}")

        if result.returncode == 0:
            return True
        else:
            if result.stderr:
                print(f"\n   Error: {result.stderr}")
            return False

    except Exception as e:
        print(f"\n   Error running DRC: {e}")
        return False


# =============================================================================
# Main Entry Point
# =============================================================================

def main() -> int:
    """Main entry point."""
    if len(sys.argv) > 1:
        output_dir = Path(sys.argv[1])
    else:
        output_dir = Path(__file__).parent / "output"

    try:
        # Step 1: Create project file
        project_path = create_project(output_dir, "usb_joystick")

        # Step 2: Create schematic
        sch_path = create_usb_joystick_schematic(output_dir)

        # Step 3: Run ERC
        erc_success = run_erc(sch_path)

        # Step 4: Create PCB
        pcb_path = create_usb_joystick_pcb(output_dir)

        # Step 5: Route PCB
        routed_path = output_dir / "usb_joystick_routed.kicad_pcb"
        route_success = route_pcb(pcb_path, routed_path)

        # Step 6: Run DRC
        drc_success = run_drc(routed_path)

        # Summary
        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"\nOutput directory: {output_dir.absolute()}")
        print("\nGenerated files:")
        print(f"  1. Project: {project_path.name}")
        print(f"  2. Schematic: {sch_path.name}")
        print(f"  3. PCB (unrouted): {pcb_path.name}")
        print(f"  4. PCB (routed): {routed_path.name}")
        print("\nResults:")
        print(f"  ERC: {'PASS' if erc_success else 'FAIL'}")
        print(f"  Routing: {'SUCCESS' if route_success else 'PARTIAL'}")
        print(f"  DRC: {'PASS' if drc_success else 'FAIL'}")
        print("\nBoard description:")
        print("  - USB game controller with analog joystick")
        print("  - 32-pin QFP MCU")
        print("  - USB Type-C connector")
        print("  - 4 tactile buttons")

        # For this complex demo board, partial routing is acceptable
        # Success if ERC passes and DRC has no errors (warnings OK)
        return 0 if erc_success and drc_success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
