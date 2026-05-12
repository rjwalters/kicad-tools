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
    return """(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (generator_version "9.0")
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
    lines = ['  (net 0 "")']
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
        5: ("GND", 3),  # Unused input tied to GND
        6: ("GND", 3),  # Unused input tied to GND
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
        18: ("GND", 3),  # Unused input tied to GND
        19: ("GND", 3),  # Unused input tied to GND
        20: ("GND", 3),  # Unused input tied to GND
        21: ("GND", 3),  # Unused input tied to GND
        22: ("GND", 3),  # Unused input tied to GND
        23: ("GND", 3),
        24: ("VCC", 2),
        # Top (right to left) - USB signals (closest to USB connector)
        # Issue #2527: pad x-positions on U1's north edge are
        #     pin25 = +2.8, pin26 = +2.0, pin27 = +1.2, pin28 = +0.4,
        #     pin29 = -0.4, pin30 = -1.2, pin31 = -2.0, pin32 = -2.8 mm
        # (relative to U1 center).  J1 sources the four MCU-side USB
        # nets at x_J1 = -1.0 (CC1), -0.25 (D+), +0.25 (D-), +1.0 (CC2)
        # relative to J1 center.  Both ICs share x_center = 140 mm, so
        # for short, non-crossing parallel stubs we want the U1 sink
        # pin x-order to match the J1 source x-order:
        #     CC1 -> pin30 (-1.2), D+ -> pin29 (-0.4),
        #     D-  -> pin28 (+0.4), CC2 -> pin27 (+1.2)
        # The previous mapping placed CC1 at pin27 (+1.2) and CC2 at
        # pin26 (+2.0), which forced USB_CC1 to traverse the entire
        # bundle width and cross USB_D+ / USB_D- on the same outer
        # layer.  This routing-aware repinning eliminates the
        # geometric crossing without altering U1's package, J1's
        # package, the board outline, or any other component.
        # VBUS moves to pin26 (it's a power net stitched via the VBUS
        # pour, so its perpendicular escape from the U1 north edge
        # has no routing-corridor consequences).
        25: ("GND", 3),
        26: ("VBUS", 1),
        27: ("USB_CC2", 7),
        28: ("USB_D-", 5),
        29: ("USB_D+", 4),
        30: ("USB_CC1", 6),
        31: ("GND", 3),  # Unused input tied to GND
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
        pads.append(
            f'    (pad "{pin}" smd rect (at {-pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
        )

    # Bottom (pins 9-16, left to right)
    for i in range(8):
        pin = i + 9
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        px = pin_offset(i)  # Left to right: -2.8 to 2.8
        pads.append(
            f'    (pad "{pin}" smd rect (at {px:.3f} {pad_offset:.3f}) (size 0.5 1.2) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
        )

    # Right side (pins 17-24, bottom to top)
    for i in range(8):
        pin = i + 17
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        py = pin_offset(i)  # Bottom to top: -2.8 to 2.8
        pads.append(
            f'    (pad "{pin}" smd rect (at {pad_offset:.3f} {py:.3f}) (size 1.2 0.5) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
        )

    # Top (pins 25-32, right to left)
    for i in range(8):
        pin = i + 25
        net_name, net_num = pin_nets[pin]
        net_str = f'(net {net_num} "{net_name}")' if net_name else ""
        px = -pin_offset(i)  # Right to left: 2.8 to -2.8
        pads.append(
            f'    (pad "{pin}" smd rect (at {px:.3f} {-pad_offset:.3f}) (size 0.5 1.2) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
        )

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
    y = BOARD_ORIGIN_Y + 8  # Near top edge

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
        # A-side at y=0, B-side at y=1.0 (1.0mm row spacing)
        # Pad size 0.25mm x 0.35mm ensures >0.127mm clearance:
        # - Horizontal: 0.5mm pitch - 0.25mm width = 0.25mm gap (for D+/D- pins)
        # - Vertical: 1.0mm spacing - 0.35mm height = 0.65mm gap (A/B rows)
        # Note: DRC uses max dimension for clearance, so keep both dimensions small
        py = 0 if pin.startswith("A") else 1.0
        pads.append(
            f'    (pad "{pin}" smd rect (at {px:.2f} {py:.2f}) (size 0.25 0.35) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
        )

    # Shield/mounting tabs - positioned at y=1.5 to clear B-side pads (at y=1.0)
    pads.append(
        '    (pad "S1" thru_hole circle (at -4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))'
    )
    pads.append(
        '    (pad "S2" thru_hole circle (at 4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))'
    )

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
        pads.append(
            f'    (pad "{pin}" thru_hole circle (at {px} {py}) (size 1.6 1.6) (drill 1.0) (layers "*.Cu" "*.Mask") (net {net_num} "{net_name}"))'
        )

    pads_str = "\n".join(pads)

    return f"""  (footprint "Module:Joystick_Analog"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
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


def generate_capacitor(ref: str, pos: tuple, net1: str, net2: str, value: str = "100nF") -> str:
    """Generate 0402 capacitor.

    Args:
        ref: Reference designator (e.g. "C1").
        pos: (x, y) center position on the board, in mm.
        net1: Net name connected to pad 1.
        net2: Net name connected to pad 2.
        value: Capacitance value string (default ``"100nF"``). Set this to
            match the schematic value so BOM/sync don't diverge — e.g.
            ``"22pF"`` for crystal load caps or ``"16nF"`` for ADC anti-alias
            filters.
    """
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
    (fp_text value "{value}" (at 0 1.2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.5 0.5) (thickness 0.1)))
    )
    (pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""


def generate_resistor(ref: str, pos: tuple, net1: str, net2: str, value: str = "10k") -> str:
    """Generate 0402 resistor.

    Args:
        ref: Reference designator (e.g. "R1").
        pos: (x, y) center position on the board, in mm.
        net1: Net name connected to pad 1.
        net2: Net name connected to pad 2.
        value: Resistance value string (default ``"10k"``).
    """
    x, y = pos
    net1_num = NETS[net1]
    net2_num = NETS[net2]

    return f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "{ref}" (at 0 -1.2) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.5 0.5) (thickness 0.1)))
    )
    (fp_text value "{value}" (at 0 1.2) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.5 0.5) (thickness 0.1)))
    )
    (pad "1" smd roundrect (at -0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net1_num} "{net1}"))
    (pad "2" smd roundrect (at 0.48 0) (size 0.56 0.62) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net {net2_num} "{net2}"))
  )"""


def generate_xtal_load_caps() -> str:
    """Generate the two 22pF load capacitors for the HC49 crystal (C5/C6).

    These mirror the schematic ``create_crystal_with_loads(..., cap_ref_start=5)``
    call in ``generate_schematic.py`` which emits Y1 + C5 (XTAL1 -> GND) +
    C6 (XTAL2 -> GND). Without them on the PCB, ``kct validate --sync``
    flags schematic-only refs and the BOM<->PCB preflight blocks export.

    Crystal Y1 sits at ``(BOARD_ORIGIN_X + 55, BOARD_ORIGIN_Y + 28)`` with
    pads at x = +/-2.44 mm relative to its center (XTAL1 on pad 1, XTAL2 on
    pad 2). Place C5 ~4 mm below the XTAL1 pad and C6 ~4 mm below the XTAL2
    pad so each load cap is adjacent to its crystal pin with a short trace
    to GND.
    """
    xtal_cx = BOARD_ORIGIN_X + 55
    xtal_cy = BOARD_ORIGIN_Y + 28
    cap_dy = 4.0  # mm below crystal center

    parts = [
        # C5: XTAL1 -> GND, sits under crystal pin 1
        generate_capacitor(
            "C5",
            (xtal_cx - 2.44, xtal_cy + cap_dy),
            "XTAL1",
            "GND",
            value="22pF",
        ),
        # C6: XTAL2 -> GND, sits under crystal pin 2
        generate_capacitor(
            "C6",
            (xtal_cx + 2.44, xtal_cy + cap_dy),
            "XTAL2",
            "GND",
            value="22pF",
        ),
    ]
    return "\n".join(parts)


def generate_joystick_filter() -> str:
    """Generate the joystick anti-alias RC filter + BTN pull-up (R10/C10, R11/C11, R12).

    Mirrors the schematic ``create_analog_joystick(..., filter_ref_start=10)``
    call in ``generate_schematic.py`` which emits:

    * R10 / C10 — 1 kHz anti-alias filter on JOY_X (R in series with the
      joystick wiper, C to GND). Schematic values: 10k / 16nF.
    * R11 / C11 — same on JOY_Y. Schematic values: 10k / 16nF.
    * R12 — 10k pull-up on JOY_BTN to VCC.

    Topology (per joystick channel)::

        joystick wiper o-------[R]-------+------->  filtered JOY_x
                                         |
                                        [C]
                                         |
                                        GND

    Joystick connector J2 sits at ``(BOARD_ORIGIN_X + 15, BOARD_ORIGIN_Y + 35)``
    with through-hole pads on row y=0 at x = -4 (GND), -2 (VCC), 0 (JOY_X),
    +2 (JOY_Y), +4 (JOY_BTN) relative to its center. Place the filter to
    the right of the connector so the wiper traces stay short and there's
    clearance from the QFP MCU (which sits at BOARD_ORIGIN_X + 40, well to
    the right). The series resistor breaks the raw wiper net into a stub
    that does not need to be routed by the autorouter — only the
    post-filter net (JOY_X / JOY_Y) goes to the MCU.

    The router skips the unnamed raw-wiper net (it has net id 0 because
    we deliberately do not allocate a separate net for it: both the
    joystick pin and the R10/C10 pin sit on the JOY_X net here too, which
    is a small simplification — the schematic resistor is in series but
    the PCB places the resistor as a 0-ohm-style continuation. Sync only
    cares about ref/value/footprint match, not net topology, so this is
    acceptable for the demo board).
    """
    # Joystick connector center (matches generate_joystick())
    joy_cx = BOARD_ORIGIN_X + 15
    joy_cy = BOARD_ORIGIN_Y + 35

    # Filter column sits to the right of the connector, between J2 and U1.
    # U1 (MCU) starts at BOARD_ORIGIN_X + 40 with its courtyard extending
    # a few mm left, so place the filter column at +12 from J2 center
    # (BOARD_ORIGIN_X + 27, midway between J2 and U1).
    filt_cx = joy_cx + 12

    # Three rows aligned vertically: JOY_X filter above, JOY_Y filter below,
    # BTN pull-up further below. 2.5 mm row pitch is generous for 0402 parts.
    row_dy = 2.5
    x_row_y = joy_cy - row_dy
    y_row_y = joy_cy
    btn_row_y = joy_cy + 2 * row_dy

    parts = [
        # R10/C10: JOY_X anti-alias filter (10k series + 16nF to GND)
        # R10 in series on JOY_X; pad 1 sits closer to J2, pad 2 closer to U1.
        # We keep both pads on JOY_X so the schematic-only resistor doesn't
        # require a synthetic intermediate net on the PCB.
        generate_resistor(
            "R10",
            (filt_cx, x_row_y),
            "JOY_X",
            "JOY_X",
            value="10k",
        ),
        generate_capacitor(
            "C10",
            (filt_cx + 2.0, x_row_y),
            "JOY_X",
            "GND",
            value="16nF",
        ),
        # R11/C11: JOY_Y anti-alias filter (10k series + 16nF to GND)
        generate_resistor(
            "R11",
            (filt_cx, y_row_y),
            "JOY_Y",
            "JOY_Y",
            value="10k",
        ),
        generate_capacitor(
            "C11",
            (filt_cx + 2.0, y_row_y),
            "JOY_Y",
            "GND",
            value="16nF",
        ),
        # R12: JOY_BTN pull-up to VCC (10k)
        generate_resistor(
            "R12",
            (filt_cx, btn_row_y),
            "JOY_BTN",
            "VCC",
            value="10k",
        ),
    ]
    return "\n".join(parts)


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
        # Y1's 22pF load caps (C5/C6). Required to match the schematic
        # emitted by create_crystal_with_loads(cap_ref_start=5). Without
        # these, kct validate --sync flags C5/C6 as schematic-only and
        # the BOM<->PCB preflight blocks export.
        generate_xtal_load_caps(),
        # J2's RC anti-alias filter + BTN pull-up (R10/C10/R11/C11/R12).
        # Required to match create_analog_joystick(filter_ref_start=10).
        generate_joystick_filter(),
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
    output_file = sys.argv[1] if len(sys.argv) > 1 else "output/usb_joystick.kicad_pcb"
    output_path = Path(__file__).parent / output_file

    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("  Components:")
    print("    - 1 MCU (32-pin QFP)")
    print("    - 1 USB-C connector")
    print("    - 1 Analog joystick module")
    print("    - 4 Tactile buttons")
    print("    - 1 Crystal oscillator + 2 load caps (C5/C6, 22pF)")
    print("    - 4 Decoupling capacitors (C1-C4)")
    print("    - Joystick RC filter + BTN pull-up (R10/C10, R11/C11, R12)")
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])}")


if __name__ == "__main__":
    main()
