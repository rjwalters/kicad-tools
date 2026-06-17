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
from kicad_tools.schematic.blocks import (
    USBConnector,
    create_analog_joystick,
    create_crystal_with_loads,
)
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
            footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm",
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
            footprint="Package_QFP:TQFP-32_7x7mm_P0.8mm",
        )
        print(f"   U1 (MCU): placed at ({mcu.x}, {mcu.y})")

    # =========================================================================
    # Section 2: Place USB connector (using autolayout + USBConnector block)
    # =========================================================================
    print("\n2. Placing USB connector via USBConnector block...")

    # Request position near top-left, let autolayout find clear spot.
    preferred_x, preferred_y = 50.8, 50.8

    # The Type-C symbol used by USBConnector by default ("USB_C_Receptacle_USB2.0")
    # is not present in every KiCad install — fall back to the 16-pin variant
    # which exposes the same VBUS / GND / D+ / D- / CC1 / CC2 / SHIELD pin
    # names that USBConnector looks up.
    usb_symbol = "Connector:USB_C_Receptacle_USB2.0_16P"

    # Use suggest_position to find a non-overlapping location for the
    # underlying Type-C receptacle symbol used by USBConnector.
    suggested_pos = sch.suggest_position(
        usb_symbol,
        near=(preferred_x, preferred_y),
        padding=5.08,  # 200mil padding
    )
    print(f"   Preferred: ({preferred_x}, {preferred_y})")
    print(f"   Suggested: {suggested_pos}")

    # USBConnector places a real Type-C receptacle symbol with typed VBUS /
    # D+ / D- / GND / CC1 / CC2 ports. We disable ESD here to preserve the
    # pre-refactor topology (no extra TVS diodes); callers wanting USB ESD
    # can flip esd_protection=True.
    usb_block = USBConnector(
        sch,
        x=suggested_pos[0],
        y=suggested_pos[1],
        connector_type="type-c",
        esd_protection=False,
        vbus_protection=False,
        ref_prefix="J1",
        connector_symbol=usb_symbol,
    )
    usb_conn = usb_block.connector
    # Preserve existing footprint assignment (matches pre-refactor PCB layout)
    usb_conn.footprint = "Connector_USB:USB_C_Receptacle_GCT_USB4105"
    print(f"   J1 (USB-C): placed at ({usb_conn.x}, {usb_conn.y})")

    # =========================================================================
    # Section 3: Place Joystick connector (uses create_analog_joystick factory)
    # =========================================================================
    print("\n3. Placing Joystick connector via create_analog_joystick factory...")

    # The factory drops the 5-pin connector and emits the VCC/GND/X/Y/BTN
    # labels.  The anti-alias RC filter (R10/C10, R11/C11) and the BTN
    # pull-up (R12) are added explicitly below instead of via the
    # factory's series-filter so the schematic netlist matches the PCB's
    # ``generate_joystick_filter()`` topology pad-for-pad (issue #3764).
    # On the PCB the filter resistors carry the SAME net on both pads
    # (JOY_X / JOY_Y) — modelled as a 0-ohm continuation rather than a
    # series element introducing an extra wiper net — so we mirror that
    # here to keep ``schematic_net_count == pcb_net_count == 16`` and
    # ``compare_netlists().clean == True``.
    joy_pos = sch.suggest_position(
        "Connector_Generic:Conn_01x05",
        near=(50.8, 101.6),
        padding=5.08,
    )
    joy_block = create_analog_joystick(
        sch,
        x=joy_pos[0],
        y=joy_pos[1],
        ref="J2",
        vcc_net="VCC",
        gnd_net="GND",
        x_net="JOY_X",
        y_net="JOY_Y",
        btn_net="JOY_BTN",
        # Filter + pull-up are emitted explicitly below (see note above);
        # disable the factory's series filter so it labels the raw
        # connector pins JOY_X / JOY_Y / JOY_BTN directly.
        filter_cutoff_hz=None,
        btn_pullup=None,
    )
    joy_conn = joy_block.connector
    print(f"   J2 (Joystick): placed at ({joy_conn.x}, {joy_conn.y})")

    # Explicit RC anti-alias filter + BTN pull-up, mirroring
    # ``generate_pcb.py:generate_joystick_filter()`` net-for-net:
    #   R10 (JOY_X / JOY_X) + C10 (JOY_X / GND)
    #   R11 (JOY_Y / JOY_Y) + C11 (JOY_Y / GND)
    #   R12 (JOY_BTN / VCC)  -- pull-up to VCC
    joy_filter_specs = [
        ("R10", "Device:R", "10k", "Resistor_SMD:R_0402_1005Metric", "JOY_X", "JOY_X"),
        ("C10", "Device:C", "16nF", "Capacitor_SMD:C_0402_1005Metric", "JOY_X", "GND"),
        ("R11", "Device:R", "10k", "Resistor_SMD:R_0402_1005Metric", "JOY_Y", "JOY_Y"),
        ("C11", "Device:C", "16nF", "Capacitor_SMD:C_0402_1005Metric", "JOY_Y", "GND"),
        ("R12", "Device:R", "10k", "Resistor_SMD:R_0402_1005Metric", "JOY_BTN", "VCC"),
    ]
    filt_base_x, filt_base_y = joy_conn.x + 25.4, joy_conn.y
    for i, (ref, sym, val, fp, net1, net2) in enumerate(joy_filter_specs):
        fpos = sch.suggest_position(sym, near=(filt_base_x, filt_base_y + i * 7.62), padding=2.54)
        comp = sch.add_symbol(sym, x=fpos[0], y=fpos[1], ref=ref, value=val, footprint=fp)
        p1 = comp.pin_position("1")
        p2 = comp.pin_position("2")
        if p1:
            add_pin_label(sch, p1, net1, direction="left")
        if p2:
            add_pin_label(sch, p2, net2, direction="right")
        print(f"   {ref}: {net1}/{net2} at ({comp.x}, {comp.y})")

    # =========================================================================
    # Section 4: Place Crystal with load capacitors
    # =========================================================================
    print("\n4. Placing Crystal with load capacitors...")

    # Crystal should be near MCU. Use create_crystal_with_loads to instantiate
    # the crystal AND its two 22pF load caps in one shot - previously this
    # board placed only the bare crystal, leaving the load caps floating
    # (an electrical bug for any real 16 MHz HSE oscillator).
    xtal_pos = sch.suggest_position(
        "Device:Crystal",
        near=(127.0, 76.2),  # Near MCU
        padding=5.08,
    )

    xtal_block = create_crystal_with_loads(
        sch,
        x=xtal_pos[0],
        y=xtal_pos[1],
        frequency="16MHz",
        load_pF=22,
        cap_ref_start=5,  # C1-C4 are MCU decoupling caps; load caps become C5/C6
        crystal_footprint="Crystal:Crystal_HC49-U_Vertical",
        cap_footprint="Capacitor_SMD:C_0402_1005Metric",
    )
    xtal = xtal_block.crystal
    print(f"   Y1 (Crystal): placed at ({xtal.x}, {xtal.y})")
    print("   C5/C6 (load caps, 22pF): placed below crystal")

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
            footprint="Button_Switch_SMD:SW_SPST_TL3342",
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
        # C4: VBUS bypass for the USB connector. Wired to VCC/GND via the
        # generic decoupling loop below (functionally identical to a
        # DecouplingCaps(values=["100nF"]) placed on usb_block.port("VBUS")).
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
                footprint="Capacitor_SMD:C_0402_1005Metric",
            )
        except Exception:
            # Fallback if C not available
            cap = sch.add_symbol(
                "Device:R",
                x=pos[0],
                y=pos[1],
                ref=ref,
                value="100nF",
                footprint="Capacitor_SMD:C_0402_1005Metric",
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

    # MCU pin assignments — kept pad-for-pad identical to the PCB's
    # ``generate_pcb.py:generate_mcu()`` ``pin_nets`` table so the
    # schematic↔PCB netlist reconciles cleanly (issue #3764).  The PCB
    # layout is the source of truth: it was routed against this exact
    # pinout (USB belt on the north edge, crystal on the west edge,
    # joystick/button GPIO on the south edge).  Every one of the 32 pins
    # carries a real net — there are no no-connects on U1.
    MCU_PIN_MAP = {
        # Left side (pins 1-8): GND / crystal / VCC
        "1": "GND",
        "2": "XTAL1",  # Crystal in
        "3": "XTAL2",  # Crystal out
        "4": "VCC",
        "5": "GND",  # Unused input tied to GND
        "6": "GND",  # Unused input tied to GND
        "7": "GND",
        "8": "VCC",
        # Bottom (pins 9-16): joystick + button GPIO
        "9": "JOY_X",  # ADC0 - Joystick X axis
        "10": "JOY_Y",  # ADC1 - Joystick Y axis
        "11": "JOY_BTN",  # Joystick push-button
        "12": "BTN1",
        "13": "BTN2",
        "14": "BTN3",
        "15": "BTN4",
        "16": "GND",
        # Right side (pins 17-24): power / unused-to-GND
        "17": "VCC",
        "18": "GND",
        "19": "GND",
        "20": "GND",
        "21": "GND",
        "22": "GND",
        "23": "GND",
        "24": "VCC",
        # Top (pins 25-32): USB belt (matches the routed north-edge order)
        "25": "GND",
        "26": "VBUS",
        "27": "USB_CC2",
        "28": "USB_D-",
        "29": "USB_D+",
        "30": "USB_CC1",
        "31": "GND",  # Unused input tied to GND
        "32": "GND",
    }

    # Joystick pin labels (VCC/GND/JOY_X/JOY_Y/JOY_BTN) are emitted by
    # ``create_analog_joystick`` in Section 3 — no inline wiring needed here.

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

    # Wire USB connector pins via the underlying symbol. The 16P Type-C
    # receptacle exposes multiple physical pins per logical signal (e.g.
    # two D+ pins for cable-orientation mux). Iterate the symbol's pins
    # and emit a global label per pin so all instances share the same net.
    print("   Wiring USB connector (J1) pins...")
    # Pin-name -> net mapping.  Matches the PCB's J1 footprint nets in
    # ``generate_pcb.py:generate_usb_connector()`` pad-for-pad (issue
    # #3764): VBUS, USB_CC1, USB_CC2 are DISTINCT nets (a real Type-C
    # model), not folded into VCC/GND.  Pins mapped to ``None`` are
    # emitted as no-connects to match the PCB pads that carry no net.
    USB_PIN_NET_MAP = {
        "VBUS": "VBUS",
        "GND": "GND",
        "D+": "USB_D+",
        "D-": "USB_D-",
        # CC1/CC2 are distinct configuration-channel nets routed to the
        # MCU (U1 pins 30/27).  The PCB exposes them as USB_CC1/USB_CC2.
        "CC1": "USB_CC1",
        "CC2": "USB_CC2",
        # Connector shield tied to GND (PCB S1/S2 mounting tabs -> GND).
        "SHIELD": "GND",
        # SBU side-band pins are unused for USB 2.0; the PCB leaves the
        # corresponding A8/B8 pads with no net, so mark them NC here.
        "SBU1": None,
        "SBU2": None,
    }
    for pin in usb_conn.symbol_def.pins:
        pin_pos = usb_conn.pin_position(pin.number)
        if not pin_pos:
            continue
        net_name = USB_PIN_NET_MAP.get(pin.name)
        if net_name is None:
            sch.add_no_connect(pin_pos[0], pin_pos[1], snap=False)
            print(f"      Pin {pin.number} ({pin.name}) -> NC")
        else:
            add_pin_label(sch, pin_pos, net_name, direction="right")
            print(f"      Pin {pin.number} ({pin.name}) -> {net_name}")

    # Joystick (J2) wiring is handled by create_analog_joystick (Section 3).

    # Wire crystal pins (load caps already wired internally by the block).
    # Connect the load-cap ground bus to the GND rail and label IN/OUT for
    # routing back to the MCU XTAL1/XTAL2 pins.
    print("   Wiring Crystal (Y1) pins...")
    # Label the load-cap ground bus as GND so C5/C6 pad 2 resolve to the
    # GND net (matches PCB C5-2/C6-2 = GND).  ``connect_to_rails`` only
    # draws a wire to a bare y-coordinate with no net label, which left
    # the load caps on auto-named ``Net-(C5-2)`` nets and broke LVS
    # (issue #3764).
    add_pin_label(sch, xtal_block.port("GND"), "GND", direction="right")
    add_pin_label(sch, xtal_block.port("IN"), "XTAL1", direction="left")
    print("      IN -> XTAL1")
    add_pin_label(sch, xtal_block.port("OUT"), "XTAL2", direction="right")
    print("      OUT -> XTAL2")

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

    # Wire decoupling capacitors.  C1-C3 are VCC->GND MCU decoupling.
    # C4 is the USB VBUS input bypass cap, so its pad 1 ties to VBUS
    # (matching the PCB's ``("C4", ..., "VBUS", "GND")`` placement) — not
    # VCC.  Tying it to VCC was part of the schematic↔PCB drift fixed in
    # issue #3764.
    print("   Wiring Decoupling Capacitors (C1-C4)...")
    for cap in caps:
        # Capacitors have 2 pins - pin 1 to the rail, pin 2 to GND
        pad1_net = "VBUS" if cap.reference == "C4" else "VCC"
        pin1_pos = cap.pin_position("1")
        pin2_pos = cap.pin_position("2")
        if pin1_pos:
            add_pin_label(sch, pin1_pos, pad1_net, direction="left")
            print(f"      {cap.reference} Pin 1 -> {pad1_net}")
        if pin2_pos:
            add_pin_label(sch, pin2_pos, "GND", direction="right")
            print(f"      {cap.reference} Pin 2 -> GND")

    # Add power symbols with global labels
    print("   Adding power symbols...")

    # Add PWR_FLAG to indicate power entry points
    # VCC power flag near top-left with global label.  Use a ``power:VCC``
    # symbol (not ``power:+5V``) so the schematic exposes the VCC rail the
    # PCB actually uses and does NOT leak a spurious ``+5V`` global net
    # (issue #3764 — the ``+5V`` drift was one of the ship-ready blockers).
    vcc_pwr = sch.add_power("power:VCC", x=25.4, y=RAIL_VCC, rotation=0)
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
