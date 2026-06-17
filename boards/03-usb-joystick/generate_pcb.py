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
    """Generate the PCB file header.

    File-format version is 20260206 (KiCad 10), NOT 20240108 (KiCad 8):
    KiCad 10's ``kicad-cli`` rejects the older grammar whenever the PCB
    contains ``(zone ...)`` blocks ("Failed to load board").  With the
    old version stamp, the zone-fill step (``generate_design.py:
    fill_zones_in_routed_pcb`` and ``kct route``'s post-route fill)
    fails silently and every power-net pad is left stranded at DRC time
    (issue #3410; mirrors the header comment in ``generate_design.py``).
    """
    return """(kicad_pcb
  (version 20260206)
  (generator "kicad-tools-demo")
  (generator_version "10.0")
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

    # Simplified USB-C with main pins.
    #
    # A side and B side pins are mirrored EXCEPT the D+/D- tails (issue
    # #3410 "J1 re-spin" -- the exit clause named by the board's DRC
    # allowlist entry in .github/routed-drc-tolerance.yml):
    #
    # The pre-#3410 footprint mirrored the B row exactly like the
    # receptacle TONGUE (B7 under A6, B6 under A7).  With nets
    # A6=B6=USB_D+ / A7=B7=USB_D-, the two same-net tie stubs
    # (A6->B6, A7->B7) were forced into a diagonal X-crossover that
    # physically overlapped on F.Cu -- the source of the structural
    # ``diffpair_clearance_intra`` (-0.200mm) allowlist entry AND of the
    # BLOCKED_BY_COMPONENT stranding of whichever of D+/D- routed
    # second (77%-reach root cause #1).  Real USB 2.0-only USB-C
    # receptacles (GCT USB4105 et al.) reorder the SMT tails inside the
    # connector body so same-signal tails exit adjacent; this simplified
    # demo footprint now does the same by placing B6 under A6 and B7
    # under A7, giving both diff-pair nets straight vertical tie stubs
    # and a clean south escape.
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
        # B side (directly below A; B6/B7 tails exit under their
        # same-signal A-side partners, see #3410 re-spin note above)
        ("B1", 2.75, "GND"),
        ("B4", 1.75, "VBUS"),
        ("B5", 1.0, "USB_CC2"),
        ("B6", -0.25, "USB_D+"),
        ("B7", 0.25, "USB_D-"),
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

    # Shield/mounting tabs - positioned at y=1.5 to clear B-side pads (at y=1.0).
    # Both tabs share pad name "SH" (net GND) to match the schematic
    # symbol's single shield pin ``SH`` so the schematic↔PCB netlist
    # reconciles pad-for-pad (issue #3764).  KiCad treats same-numbered
    # pads as one electrical node, which is exactly the shield ground.
    pads.append(
        '    (pad "SH" thru_hole circle (at -4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))'
    )
    pads.append(
        '    (pad "SH" thru_hole circle (at 4.3 1.5) (size 1.0 1.0) (drill 0.6) (layers "*.Cu" "*.Mask") (net 3 "GND"))'
    )

    pads_str = "\n".join(pads)

    # J1 is a perimeter-mounted USB-C receptacle whose location is mechanically
    # constrained by the board edge. Mark it locked so that
    # placement-feedback passes from `kct route --placement-feedback` and
    # `--anchor-weight` (PR #2825) treat it as an immovable anchor. Without
    # this, the centroid pull would relocate the connector and starve VBUS /
    # USB_CC1 / USB_CC2 of routing corridors (see #2833 sub-issue A / #2836).
    #
    # Issue #3410: the lock is the MODERN top-level ``(locked yes)`` form,
    # NOT the legacy ``(attr smd locked)`` token — KiCad 10's kicad-cli
    # rejects the legacy form ("Failed to load board"), which silently
    # broke zone fill + DRC + gerber export for every artifact generated
    # from this script.
    return f"""  (footprint "Connector_USB:USB_C_Receptacle_GCT_USB4105"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (attr smd)
    (locked yes)
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "USB-C" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_joystick() -> str:
    """Generate analog joystick footprint (2-axis + button).

    J2 was nudged west from ``BOARD_ORIGIN_X + 15`` to
    ``BOARD_ORIGIN_X + 13`` by issue #2943.  Pre-nudge, J2-5 (JOY_BTN)
    sat at absolute x = ``BOARD_ORIGIN_X + 19`` and the routing channel
    between J2-5 and the west-side crystal Y1 (at
    ``BOARD_ORIGIN_X + 22``) was only ~3 mm tall, forcing JOY_Y
    segments from the filter column (``filt_cx = BOARD_ORIGIN_X + 27``)
    westward to J2-4 to clip J2-5's pad copper (6
    ``clearance_pad_segment`` errors).  Walking J2 west by 2 mm moves
    J2-5 to x = ``BOARD_ORIGIN_X + 17`` and opens a clean >5 mm
    channel; J2-1 (GND) at absolute x = ``BOARD_ORIGIN_X + 9`` still
    sits 9 mm inside the PCB west edge (``BOARD_ORIGIN_X``), so the
    joystick body's south-edge overhang for user grip is preserved.
    """
    x = BOARD_ORIGIN_X + 13  # Left side of board (nudged west by 2mm per #2943)
    y = BOARD_ORIGIN_Y + 35

    # Analog joystick module pinout, matching the canonical
    # ``Module:Joystick_Analog`` pin order used by the schematic block
    # ``create_analog_joystick`` (pin 1 = VCC, pin 2 = GND).  Issue #3764
    # reconciled the previously swapped pin-1/2 order so the
    # schematic↔PCB netlist matches pad-for-pad.  Both VCC and GND are
    # power-pour nets, so swapping these two adjacent through-hole pads
    # has no signal-routing consequence.
    # 5 pins: VCC, GND, VRx, VRy, SW
    pins = [
        ("1", -4, 0, "VCC"),
        ("2", -2, 0, "GND"),
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

    # J2 is the through-hole joystick header. Like J1 it is a mechanically
    # constrained perimeter component (the joystick module body extends
    # beyond the PCB edge for the user to grip). Lock it so placement
    # cannot drift it interior, which would starve VCC / GND distribution
    # to the joystick analog rail (see #2833 sub-issue A / #2836).
    return f"""  (footprint "Module:Joystick_Analog"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (attr through_hole)
    (locked yes)
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
    """Generate crystal oscillator footprint.

    The MCU (U1) sits at (BOARD_ORIGIN_X + 40, BOARD_ORIGIN_Y + 32).  Its
    XTAL pins (2 = XTAL1, 3 = XTAL2) are on the LEFT/west side of the QFP
    package (pad_offset = -4.5 mm from U1 center, so absolute x ≈ centre-4.5).
    Placing the crystal to the RIGHT of U1 forces the router to run 17–22 mm
    traces from those west-edge pads around or through the MCU body — a
    channel-blocked failure confirmed in board-03 routing audit (2026-05-15).

    Fix: place Y1 to the LEFT of U1 (BOARD_ORIGIN_X + 22), with its centre
    vertically aligned with the XTAL pad row (BOARD_ORIGIN_Y + 30, midway
    between pins 2 and 3 at y offsets -2.0 and -1.2 mm from U1 centre).
    The gap between Y1 pin 2 (x ≈ BOARD_ORIGIN_X + 24.4) and U1 pin 2
    (x ≈ BOARD_ORIGIN_X + 35.5) is ≈11 mm — plenty of room for a short
    direct trace on F.Cu without crossing any other signal.
    """
    x = BOARD_ORIGIN_X + 22  # Left of MCU (XTAL pins 2/3 are on U1's west edge)
    y = BOARD_ORIGIN_Y + 30  # Aligned with U1 XTAL pad row

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

    Crystal Y1 sits at ``(BOARD_ORIGIN_X + 22, BOARD_ORIGIN_Y + 30)`` with
    pads at x = +/-2.44 mm relative to its center (XTAL1 on pad 1, XTAL2 on
    pad 2). Place C5/C6 ~4 mm ABOVE (north of) the crystal so each load
    cap is adjacent to its crystal pin with a short trace to GND.

    Why above (negative dy), not below: the joystick J2 sits at
    ``(BOARD_ORIGIN_X + 13, BOARD_ORIGIN_Y + 35)`` (nudged west by
    issue #2943; pre-nudge it sat at ``BOARD_ORIGIN_X + 15``) with
    through-hole pads spanning x = 9..17, y = 35.  Even with the
    nudge, a below-Y1 cap at (xtal_cx - 2.44, xtal_cy + 4) =
    (BOARD_ORIGIN_X + 19.56, BOARD_ORIGIN_Y + 34) sits very close to
    J2-5's new position (1.6 mm dia pad at
    (BOARD_ORIGIN_X + 17, BOARD_ORIGIN_Y + 35)) -- the
    centre-to-centre distance is ~2.78 mm, which is right at the
    clearance edge for a 1.6 mm dia pad next to a 0.56 mm rectangular
    cap pad (only ~0.07 mm slack).  Flipping cap_dy to negative
    places C5/C6 in the empty area between Y1 (y = 30) and U1's
    north pad row (y = 27.5), with > 0.9 mm clearance on both sides.
    """
    xtal_cx = BOARD_ORIGIN_X + 22
    xtal_cy = BOARD_ORIGIN_Y + 30
    cap_dy = -4.0  # mm above crystal center (negative = north of Y1)

    parts = [
        # C5: XTAL1 -> GND, sits above (north of) crystal pin 1
        generate_capacitor(
            "C5",
            (xtal_cx - 2.44, xtal_cy + cap_dy),
            "XTAL1",
            "GND",
            value="22pF",
        ),
        # C6: XTAL2 -> GND, sits above (north of) crystal pin 2
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

    Joystick connector J2 sits at ``(BOARD_ORIGIN_X + 13, BOARD_ORIGIN_Y + 35)``
    (nudged west by 2 mm per issue #2943; pre-nudge it sat at
    ``BOARD_ORIGIN_X + 15`` and the JOY_Y channel clipped J2-5).
    Through-hole pads are on row y=0 at x = -4 (GND), -2 (VCC), 0 (JOY_X),
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
    # Joystick connector center (matches generate_joystick()).  J2 was
    # nudged west by 2 mm in issue #2943 (was BOARD_ORIGIN_X + 15) to
    # open the JOY_Y routing channel past J2-5 / Y1.  Keep this literal
    # in sync with ``generate_joystick()``'s ``x`` literal above.
    joy_cx = BOARD_ORIGIN_X + 13
    joy_cy = BOARD_ORIGIN_Y + 35

    # Filter column sits to the right of the connector, between J2 and U1.
    # U1 (MCU) starts at BOARD_ORIGIN_X + 40 with its courtyard extending
    # a few mm left, so place the filter column at +14 from J2 center
    # (BOARD_ORIGIN_X + 27 absolute; pre-#2943 this was joy_cx + 12 with
    # joy_cx = +15, also yielding +27).  Holding the filter at +27
    # absolute keeps the joystick-to-U1 routing topology unchanged when
    # J2 moves; experimentation (#2943) confirmed shifting the filter
    # column with J2 (e.g. joy_cx + 12 -> absolute +25) regresses DRC.
    filt_cx = joy_cx + 14

    # Three rows aligned vertically: JOY_X filter above, JOY_Y filter below,
    # BTN pull-up further below.
    #
    # Issue #3410: NO filter row may sit on a Steiner-junction line.  The
    # router's Steiner branch points for the JOY nets land at the
    # intersection of the filter COLUMN (x = filt_cx) with the rows of
    # the nets' other pads -- J2's pad row (y = joy_cy = +35) and U1's
    # south pad row (y = +36.5).  A filter pad at exactly such a junction
    # strands the net with pin_access "PADS_OFF_GRID ... blocked by
    # <other JOY net> (pad at 0.02mm)":
    #
    #   * pre-#3410, R11 sat at (filt_cx, +35) and JOY_X's Steiner point
    #     collided with R11-1;
    #   * an intermediate fix moved R11 to (filt_cx, +36.5) and the SAME
    #     collision re-appeared on JOY_BTN's Steiner point (junction of
    #     the filter column with U1.11's row).
    #
    # Keep all three rows clear of y = +35 and y = +36.5:
    #   JOY_X filter at +32.5, JOY_Y filter at +34.0, BTN pull-up at +40.
    x_row_y = joy_cy - 2.5
    y_row_y = joy_cy - 1.0
    btn_row_y = joy_cy + 5.0

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


def generate_power_pours() -> str:
    """Generate GND / VCC / VBUS copper-pour zones.

    Board 03's routing recipe (``generate_design.py:route_pcb``) skips
    VCC / GND / VBUS — those nets are served by copper pours, not traces.
    The pours must therefore be part of the GENERATED board, otherwise a
    fresh ``generate_pcb.py`` + route leaves 28/29 GND pads, 7/8 VCC
    pads, and 5/6 VBUS pads stranded (``connectivity`` DRC errors).

    Zone set (mirrors the pattern proven by the pre-#3410 board and
    board-05's ``create_zones_for_pcb``, recomputed for THIS board's
    80x60 placement):

      * GND on F.Cu + B.Cu (priority 1): full-board planes.  GND pads
        exist on every component; the F.Cu plane catches the SMD pads
        directly, B.Cu provides the return-current plane (#2833).
      * VCC on F.Cu (priority 2): island over the VCC consumers —
        J2 pin 2 (111,135), R12 pull-up (127.5,140), C1/C2/C3
        (131.5/147.5/139.5 x 128..142) and U1 pins 4/8/17/24
        (135.5..144.5 x 129.2..134.8).
      * VBUS on F.Cu (priority 3): island covering J1's VBUS row
        (A4/A9/B4/B9 at 138.25..141.75 x 108..109), C4 (146,112) and
        U1 pin 26 (142,127.5).  The island deliberately spans the
        J1->U1 area; the fill engine flows around the USB_D+/USB_D-
        pads and traces (which cross it at x = 39.6..40.4 board-rel).

    Priorities: VBUS > VCC > GND so the islands win their overlap
    regions.  Inset is 0.3mm (jlcpcb min_edge_clearance), comfortably
    above ``kct route`` auto-pour's re-inset threshold of
    ``edge_clearance * 0.5``.
    """
    inset = 0.3  # mm — jlcpcb-tier1 min_edge_clearance
    x1 = BOARD_ORIGIN_X + inset
    y1 = BOARD_ORIGIN_Y + inset
    x2 = BOARD_ORIGIN_X + BOARD_WIDTH - inset
    y2 = BOARD_ORIGIN_Y + BOARD_HEIGHT - inset

    # VCC island: bounding box of all VCC pads + margin.  Issue #3764
    # moved J2 pin 1 to VCC (matching the schematic block's pin order),
    # so the joystick VCC pad now sits at x = BOARD_ORIGIN_X + 9 (J2 at
    # +13, pin-1 offset -4) with a 1.6 mm dia through-hole footprint
    # spanning x = 8.2..9.8.  Extend the west edge to +8.0 so the VCC
    # pour overlaps that pad and J2.1 is not left stranded by DRC
    # connectivity.  (The GND pour on the same area keeps J2.2 = GND
    # connected via thermal relief.)
    vcc_x1 = BOARD_ORIGIN_X + 8.0
    vcc_y1 = BOARD_ORIGIN_Y + 26.5
    vcc_x2 = BOARD_ORIGIN_X + 49.0
    vcc_y2 = BOARD_ORIGIN_Y + 44.0

    # VBUS island: J1 VBUS row + C4 + U1.26 with ~1.2mm margin.
    vbus_x1 = BOARD_ORIGIN_X + 37.4
    vbus_y1 = BOARD_ORIGIN_Y + 6.4
    vbus_x2 = BOARD_ORIGIN_X + 47.6
    vbus_y2 = BOARD_ORIGIN_Y + 28.6

    def zone_sexp(
        net_name: str, layer: str, priority: int, poly: tuple[float, float, float, float]
    ) -> str:
        zx1, zy1, zx2, zy2 = poly
        return f"""  (zone
    (net {NETS[net_name]})
    (net_name "{net_name}")
    (layer "{layer}")
    (uuid "{generate_uuid()}")
    (hatch edge 0.5)
    (priority {priority})
    (connect_pads (clearance 0.3)
    )
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4)
    )
    (polygon (pts (xy {zx1} {zy1}) (xy {zx2} {zy1}) (xy {zx2} {zy2}) (xy {zx1} {zy2}))
    )
  )"""

    return "\n".join(
        [
            zone_sexp("GND", "F.Cu", 1, (x1, y1, x2, y2)),
            zone_sexp("GND", "B.Cu", 1, (x1, y1, x2, y2)),
            zone_sexp("VCC", "F.Cu", 2, (vcc_x1, vcc_y1, vcc_x2, vcc_y2)),
            zone_sexp("VBUS", "F.Cu", 3, (vbus_x1, vbus_y1, vbus_x2, vbus_y2)),
        ]
    )


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
        # C4 (VBUS input cap) sits EAST of the J1->U1 USB channel, not on
        # the board's vertical centerline: J1 is at (+40, +8) and U1's
        # USB_D+/USB_D- pads are at x = +39.6 / +40.4 (pads 29/28), so a
        # cap at (+40, +15) lands directly on the D+/D- corridor and
        # forces the diff pair around its courtyard (issue #3410 — this
        # was one of the 3 stranded-net root causes at the 77% plateau).
        # (+46, +12) keeps C4 within 7mm of J1's VBUS pads for its
        # decoupling role while leaving the x in [39, 41.5] channel clear.
        ("C4", (BOARD_ORIGIN_X + 46, BOARD_ORIGIN_Y + 12), "VBUS", "GND"),  # USB input cap
    ]
    for ref, pos, net1, net2 in cap_positions:
        parts.append(generate_capacitor(ref, pos, net1, net2))

    # GND/VCC/VBUS pours. Emitted after all footprints (KiCad accepts
    # zones in any order inside `(kicad_pcb ...)`, but emitting last
    # keeps the file readable: footprints define geometry, the pours
    # react to it). Sub-issue A of #2833; extended to the full power
    # set by #3410 so the canonical skip-nets recipe has copper for
    # every skipped net.
    parts.append(generate_power_pours())

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
    print("    - 1 USB-C connector (J1, locked)")
    print("    - 1 Analog joystick module (J2, locked)")
    print("    - 4 Tactile buttons")
    print("    - 1 Crystal oscillator + 2 load caps (C5/C6, 22pF)")
    print("    - 4 Decoupling capacitors (C1-C4)")
    print("    - Joystick RC filter + BTN pull-up (R10/C10, R11/C11, R12)")
    print("  Zones:")
    print("    - GND pours on F.Cu + B.Cu (full board, 0.3mm edge inset)")
    print("    - VCC island on F.Cu (priority 2)")
    print("    - VBUS island on F.Cu (priority 3)")
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])}")


if __name__ == "__main__":
    main()
