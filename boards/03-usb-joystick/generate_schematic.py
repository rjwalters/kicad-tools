#!/usr/bin/env python3
"""
Generate a KiCad Schematic for the USB Joystick Controller using autolayout.

This script demonstrates the kicad-tools schematic API and autolayout functionality:
- Symbol placement with auto_layout=True to avoid overlaps
- suggest_position() for finding non-overlapping positions
- find_overlapping_symbols() for validation

Usage:
    python generate_schematic.py [output_file]
"""

import sys
from pathlib import Path

from kicad_tools.schematic.models.schematic import Schematic, SnapMode


def create_usb_joystick_schematic(output_path: Path) -> bool:
    """
    Create a USB Joystick schematic demonstrating autolayout features.

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
        grid=2.54,  # Standard 100mil grid
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
    # Section 7: Add Power Symbols
    # =========================================================================
    print("\n7. Adding power symbols...")

    sch.add_power("power:+5V", x=25.4, y=RAIL_VCC, rotation=0)
    sch.add_power("power:GND", x=25.4, y=RAIL_GND, rotation=180)
    print("   Added +5V and GND power symbols")

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
    # Section 9: Add some basic wiring (rails)
    # =========================================================================
    print("\n9. Adding power rails...")

    # VCC rail
    sch.add_wire((25.4, RAIL_VCC), (177.8, RAIL_VCC))
    sch.add_label("+5V", 25.4, RAIL_VCC)

    # GND rail
    sch.add_wire((25.4, RAIL_GND), (177.8, RAIL_GND))
    sch.add_label("GND", 25.4, RAIL_GND)

    print("   Added VCC and GND rails")

    # =========================================================================
    # Section 10: Validate and Write
    # =========================================================================
    print("\n10. Validating schematic...")

    issues = sch.validate()
    errors = [i for i in issues if i["severity"] == "error"]
    warnings = [i for i in issues if i["severity"] == "warning"]

    print(f"   Errors: {len(errors)}")
    print(f"   Warnings: {len(warnings)}")

    if errors:
        for err in errors[:5]:
            print(f"      ERROR: {err.get('message', err)}")

    # Get statistics
    stats = sch.get_statistics()
    print("\n   Schematic statistics:")
    print(f"      Symbols: {stats['symbol_count']}")
    print(f"      Power symbols: {stats['power_symbol_count']}")
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
    if len(sys.argv) > 1:
        output_path = Path(sys.argv[1])
    else:
        output_path = Path(__file__).parent / "usb_joystick.kicad_sch"

    try:
        success = create_usb_joystick_schematic(output_path)

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
