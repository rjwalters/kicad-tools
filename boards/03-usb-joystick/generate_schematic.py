#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the USB Joystick Controller using autolayout.

This script demonstrates the kicad-tools schematic API and autolayout functionality:
- Symbol placement with auto_layout=True to avoid overlaps
- suggest_position() for finding non-overlapping positions
- find_overlapping_symbols() for validation

Usage:
    python generate_schematic.py [output_file] [-v|--verbose]
"""

import argparse
import sys
from pathlib import Path

from kicad_tools.dev import warn_if_stale
from kicad_tools.schematic.models.schematic import Schematic, SnapMode
from kicad_tools.schematic.models.validation_mixin import format_validation_summary

# Warn if running source scripts with stale pipx install
warn_if_stale()

# Wire stub length for connecting pins to labels
WIRE_STUB = 5.08  # 200 mils


def add_pin_label(sch: Schematic, pin_pos: tuple, net_name: str, direction: str = "right"):
    """
    Add a wire stub from a pin position to a global label.

    Args:
        sch: Schematic object
        pin_pos: (x, y) tuple of pin position
        net_name: Name for the global label
        direction: "left" or "right" for label placement
    """
    if not pin_pos:
        return

    x, y = pin_pos
    if direction == "right":
        end_x = x + WIRE_STUB
        rotation = 180  # Label points left toward wire
    else:
        end_x = x - WIRE_STUB
        rotation = 0  # Label points right toward wire

    # Draw wire from pin to label position
    # Use snap=False because pin positions may not be exactly on grid
    sch.add_wire((x, y), (end_x, y), snap=False)
    # Place global label at end of wire (also don't snap - must match wire endpoint)
    sch.add_global_label(net_name, end_x, y, shape="bidirectional", rotation=rotation, snap=False)


def create_usb_joystick_schematic(output_path: Path, verbose: bool = False) -> bool:
    """
    Create a USB Joystick schematic demonstrating autolayout features.

    Args:
        output_path: Path to write the schematic file
        verbose: If True, show detailed validation warnings

    Returns True if successful, False if errors occurred.
    """
    print("Creating USB Joystick Schematic with Autolayout...")
    print("=" * 60)

    # Create schematic with title block
    sch = Schematic(
        title="USB Joystick Controller",
        date="2025-01",
        revision="A",
        company="kicad-tools Demo",
        comment1="USB game controller with analog joystick",
        comment2="Demonstrates autolayout functionality",
        snap_mode=SnapMode.AUTO,
        grid=1.27,  # Standard 50mil schematic grid (matches KiCad symbol pins)
    )

    # Layout constants
    RAIL_VCC = 25.4  # Top rail for VCC
    RAIL_GND = 177.8  # Bottom rail for GND

    # =========================================================================
    # Section 1: Place MCU (central component)
    # =========================================================================
    print("\n1. Placing MCU...")

    # Place MCU in center area - this is the main component
    # Using generic connector as placeholder for 32-pin MCU
    try:
        mcu = sch.add_symbol(
            "Connector_Generic:Conn_02x16_Counter_Clockwise",
            x=101.6,  # 4" from left (in mm)
            y=88.9,  # Center vertically
            ref="U1",
            value="MCU",
        )
        print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")
    except Exception as e:
        print(f"   Warning: Could not place MCU symbol: {e}")
        print("   Using a simpler symbol...")
        mcu = sch.add_symbol(
            "Device:R",  # Fallback to simple resistor
            x=101.6,
            y=88.9,
            ref="U1",
            value="MCU",
        )
        print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    # =========================================================================
    # Section 2: Place USB connector (using autolayout)
    # =========================================================================
    print("\n2. Placing USB connector with suggest_position...")

    # Request position near top-left, let autolayout find clear spot
    preferred_x, preferred_y = 50.8, 50.8

    # Use suggest_position to find a non-overlapping location
    suggested_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x04",
        near=(preferred_x, preferred_y),
        padding=5.08,  # 200mil padding
    )
    print(f"   Preferred: ({preferred_x}, {preferred_y})")
    print(f"   Suggested: {suggested_pos}")

    usb_conn = sch.add_symbol(
        "Connector_Generic:Conn_01x04",
        x=suggested_pos[0],
        y=suggested_pos[1],
        ref="J1",
        value="USB-C",
    )
    print(f"   J1 (USB-C): placed at ({usb_conn.x}, {usb_conn.y})")

    # =========================================================================
    # Section 3: Place Joystick connector (demonstrate autolayout)
    # =========================================================================
    print("\n3. Placing Joystick connector...")

    # Place joystick connector on left side
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

    # Crystal should be near MCU
    xtal_pos = sch.suggest_position(
        "Device:Crystal",
        near=(127.0, 76.2),  # Near MCU
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
        print(f"   Y1 (Crystal): placed at ({xtal.x}, {xtal.y})")
    except Exception as e:
        print(f"   Warning: Crystal symbol not available: {e}")
        # Use resistor as placeholder
        xtal = sch.add_symbol(
            "Device:R",
            x=xtal_pos[0],
            y=xtal_pos[1],
            ref="Y1",
            value="16MHz",
        )
        print(f"   Y1 (Crystal placeholder): placed at ({xtal.x}, {xtal.y})")

    # =========================================================================
    # Section 5: Place Buttons (test multiple placements with autolayout)
    # =========================================================================
    print("\n5. Placing Buttons with autolayout (testing overlap avoidance)...")

    button_refs = ["SW1", "SW2", "SW3", "SW4"]
    base_x, base_y = 152.4, 88.9  # Right side of board

    buttons = []
    for i, ref in enumerate(button_refs):
        # Request same position for all buttons - autolayout should spread them out
        pos = sch.suggest_position(
            "Device:R",  # Using R as button placeholder
            near=(base_x, base_y),  # Same position requested for all!
            padding=7.62,  # 300mil padding for buttons
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
        ("C1", 88.9, 63.5),  # Near MCU VCC
        ("C2", 114.3, 63.5),  # Near MCU VCC
        ("C3", 88.9, 114.3),  # Near MCU GND
        ("C4", 55.88, 38.1),  # Near USB VBUS
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
            # Fallback if C not available
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
    # Section 7: Power symbols (added in Section 9 with proper wiring)
    # =========================================================================
    print("\n7. Power symbols will be added with signal wiring in Section 9...")

    # =========================================================================
    # Section 8: Check for Overlaps
    # =========================================================================
    print("\n8. Checking for symbol overlaps...")

    overlaps = sch.find_overlapping_symbols(padding=2.54)

    if overlaps:
        print(f"   WARNING: Found {len(overlaps)} overlapping symbol pairs!")
        for sym1, sym2 in overlaps:
            print(f"      {sym1.reference} overlaps {sym2.reference}")
    else:
        print("   No overlapping symbols found - autolayout working correctly!")

    # =========================================================================
    # Section 9: Add signal wiring using global labels
    # =========================================================================
    print("\n9. Adding signal wiring...")

    # Define MCU pin assignments for a typical USB microcontroller:
    # Conn_02x16_Counter_Clockwise has pins 1-16 on left, 17-32 on right
    # Pin mapping (typical USB MCU pinout):
    MCU_PIN_MAP = {
        # Power pins
        "1": "VCC",  # VCC
        "16": "GND",  # GND
        "17": "VCC",  # AVCC
        "32": "GND",  # AGND
        # USB pins
        "29": "USB_D+",  # USB D+
        "30": "USB_D-",  # USB D-
        # Crystal pins
        "7": "XTAL1",  # Crystal in
        "8": "XTAL2",  # Crystal out
        # Joystick ADC pins
        "2": "JOY_X",  # ADC0 - Joystick X axis
        "3": "JOY_Y",  # ADC1 - Joystick Y axis
        # Button GPIO pins
        "9": "BTN1",  # GPIO - Button 1
        "10": "BTN2",  # GPIO - Button 2
        "11": "BTN3",  # GPIO - Button 3
        "12": "BTN4",  # GPIO - Button 4
        "13": "JOY_BTN",  # GPIO - Joystick button
    }

    # USB connector pin assignments (4-pin USB):
    USB_PIN_MAP = {
        "1": "VCC",  # VBUS
        "2": "USB_D-",  # D-
        "3": "USB_D+",  # D+
        "4": "GND",  # GND
    }

    # Joystick connector pin assignments (5-pin):
    JOY_PIN_MAP = {
        "1": "VCC",  # VCC
        "2": "GND",  # GND
        "3": "JOY_X",  # X axis output
        "4": "JOY_Y",  # Y axis output
        "5": "JOY_BTN",  # Joystick button (optional)
    }

    # Wire MCU pins with global labels
    print("   Wiring MCU (U1) pins...")
    for pin_num, net_name in MCU_PIN_MAP.items():
        pin_pos = mcu.pin_position(pin_num)
        if pin_pos:
            # Left side pins (1-16) get labels to the left
            # Right side pins (17-32) get labels to the right
            direction = "left" if int(pin_num) <= 16 else "right"
            add_pin_label(sch, pin_pos, net_name, direction=direction)
            print(f"      Pin {pin_num} -> {net_name}")

    # Add no-connect markers for unused MCU pins
    print("   Adding no-connect markers for unused MCU pins...")
    used_pins = set(MCU_PIN_MAP.keys())
    for pin_num in range(1, 33):
        pin_str = str(pin_num)
        if pin_str not in used_pins:
            pin_pos = mcu.pin_position(pin_str)
            if pin_pos:
                sch.add_no_connect(pin_pos[0], pin_pos[1], snap=False)
                print(f"      Pin {pin_num} -> NC")

    # Wire USB connector pins
    print("   Wiring USB connector (J1) pins...")
    for pin_num, net_name in USB_PIN_MAP.items():
        pin_pos = usb_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")
            print(f"      Pin {pin_num} -> {net_name}")

    # Wire Joystick connector pins
    print("   Wiring Joystick connector (J2) pins...")
    for pin_num, net_name in JOY_PIN_MAP.items():
        pin_pos = joy_conn.pin_position(pin_num)
        if pin_pos:
            add_pin_label(sch, pin_pos, net_name, direction="right")
            print(f"      Pin {pin_num} -> {net_name}")

    # Wire crystal pins
    print("   Wiring Crystal (Y1) pins...")
    xtal_pin1 = xtal.pin_position("1")
    xtal_pin2 = xtal.pin_position("2")
    if xtal_pin1:
        add_pin_label(sch, xtal_pin1, "XTAL1", direction="left")
        print("      Pin 1 -> XTAL1")
    if xtal_pin2:
        add_pin_label(sch, xtal_pin2, "XTAL2", direction="right")
        print("      Pin 2 -> XTAL2")

    # Wire buttons to MCU GPIO pins
    print("   Wiring Buttons (SW1-SW4)...")
    button_nets = ["BTN1", "BTN2", "BTN3", "BTN4"]
    for i, (btn, net_name) in enumerate(zip(buttons, button_nets, strict=True)):
        # Each button has 2 pins - connect pin 1 to signal, pin 2 to GND
        pin1_pos = btn.pin_position("1")
        pin2_pos = btn.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, net_name, direction="left")
            print(f"      {btn.reference} Pin 1 -> {net_name}")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")
            print(f"      {btn.reference} Pin 2 -> GND")

    # Wire decoupling capacitors between VCC and GND
    print("   Wiring Decoupling Capacitors (C1-C4)...")
    for cap in caps:
        # Capacitors have 2 pins - connect pin 1 to VCC, pin 2 to GND
        pin1_pos = cap.pin_position("1")
        pin2_pos = cap.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, "VCC", direction="left")
            print(f"      {cap.reference} Pin 1 -> VCC")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")
            print(f"      {cap.reference} Pin 2 -> GND")

    # Add power symbols with global labels
    print("   Adding power symbols...")

    # Add PWR_FLAG to indicate power entry points
    # VCC power flag near top-left with global label
    vcc_pwr = sch.add_power("power:+5V", x=25.4, y=RAIL_VCC, rotation=0)
    sch.add_wire((vcc_pwr.x, vcc_pwr.y), (vcc_pwr.x + WIRE_STUB, vcc_pwr.y), snap=False)
    sch.add_global_label(
        "VCC", vcc_pwr.x + WIRE_STUB, vcc_pwr.y, shape="input", rotation=180, snap=False
    )
    sch.add_pwr_flag(vcc_pwr.x, vcc_pwr.y)

    # GND power symbol with global label
    gnd_pwr = sch.add_power("power:GND", x=25.4, y=RAIL_GND, rotation=180)
    sch.add_wire((gnd_pwr.x, gnd_pwr.y), (gnd_pwr.x + WIRE_STUB, gnd_pwr.y), snap=False)
    sch.add_global_label(
        "GND", gnd_pwr.x + WIRE_STUB, gnd_pwr.y, shape="input", rotation=180, snap=False
    )
    sch.add_pwr_flag(gnd_pwr.x, gnd_pwr.y)

    print("   Added VCC and GND power symbols with PWR_FLAG")

    # =========================================================================
    # Section 10: Validate and Write
    # =========================================================================
    print("\n10. Validating schematic...")

    issues = sch.validate()

    # Print validation summary with warning categorization
    summary = format_validation_summary(issues, verbose=verbose)
    for line in summary.split("\n"):
        print(f"   {line}")

    # Get statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
    print(f"      Global labels: {len(sch.global_labels)}")
    print(f"      Wires: {stats['wire_count']}")
    print(f"      Junctions: {stats['junction_count']}")
    print(f"      Labels: {stats['label_count']}")

    # Write output
    print(f"\n11. Writing schematic to {output_path}...")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    sch.write(output_path)
    print("   SUCCESS: Schematic written!")

    return len(overlaps) == 0


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Generate a KiCad schematic for a USB Joystick Controller"
    )
    parser.add_argument(
        "output",
        nargs="?",
        default=None,
        help="Output file path or directory (default: output/usb_joystick.kicad_sch)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed validation warnings",
    )

    args = parser.parse_args()

    # Default output filename
    default_filename = "usb_joystick.kicad_sch"

    if args.output:
        output_path = Path(args.output)
        # If user passes a directory, auto-append the default filename
        if output_path.is_dir():
            output_path = output_path / default_filename
            print(f"Note: Directory provided, using {output_path}")
    else:
        output_path = Path(__file__).parent / "output" / default_filename

    try:
        success = create_usb_joystick_schematic(output_path, verbose=args.verbose)

        print("\n" + "=" * 60)
        print("SUMMARY")
        print("=" * 60)
        print(f"Output: {output_path}")
        print(f"Result: {'SUCCESS - No overlaps' if success else 'WARNING - Overlaps found'}")

        return 0 if success else 1

    except Exception as e:
        print(f"\nError: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
