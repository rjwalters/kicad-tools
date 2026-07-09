#!/usr/bin/env python3
"""
Generate a KiCad PCB for the differential-pair test board (board 06).

This script creates a 4-layer PCB file (F.Cu / In1.Cu / In2.Cu / B.Cu)
demonstrating each protocol family of Epic #2556 Phase 4L:

- USB 2.0 (1 pair): USB-C source -> QFN-32 sink
- USB 3.0 (4 pairs: TX1/RX1/TX2/RX2): USB-C source -> BGA-49 simulator sink
- PCIe Gen1 (2 pairs: TX/RX): Mini-PCIe edge -> QFP-48 sink
- MIPI D-PHY (2 lanes: CLK/D0): FFC source -> QFN-24 sink

Net assignment follows board 03's convention (NETS dict + per-pad
``(net N "name")`` s-expr emission).  The 4-layer stackup adds
``(2 "In1.Cu" signal)`` + ``(3 "In2.Cu" signal)`` to the layers
header relative to board 03's 2-layer setup.

Usage:
    python generate_pcb.py [output_file]
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

from kicad_tools.pcb.center_sheet import centered_origin


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# Board dimensions (mm) — generous size keeps source/sink pairs well
# separated so the autorouter has plenty of channel space for each diff
# pair to route coupled without crossing other nets.
BOARD_WIDTH = 100.0
BOARD_HEIGHT = 80.0
# Sheet-center the outline: middle of the A4 sheet's usable drawing area
# (inside the 10 mm frame border, above the 35 mm title-block band).
# All placement below derives from BOARD_ORIGIN_*.
BOARD_ORIGIN_X, BOARD_ORIGIN_Y = centered_origin(BOARD_WIDTH, BOARD_HEIGHT)

# =============================================================================
# Net Definitions
# =============================================================================
# Net assignment follows board 03 convention: dict[name -> int].
# Net 0 is the implicit unconnected net.  ~25 nets total:
#   - 9 differential pairs (18 nets)
#   - 5 power rails (VBUS_USB, +3V3, +1V8, +1V2, GND)
#   - 3 single-ended sideband (USB_CC1, USB_CC2, MIPI_RST)
NETS: dict[str, int] = {
    "": 0,
    # Power rails (PWR plane = In2.Cu, GND plane = In1.Cu)
    "VBUS_USB": 1,
    "+3V3": 2,
    "+1V8": 3,
    "+1V2": 4,
    "GND": 5,
    # USB 2.0 differential pair
    "USB2_D+": 6,
    "USB2_D-": 7,
    # USB 3.0 SuperSpeed pairs (4 pairs = 8 nets)
    "USB3_TX1+": 8,
    "USB3_TX1-": 9,
    "USB3_RX1+": 10,
    "USB3_RX1-": 11,
    "USB3_TX2+": 12,
    "USB3_TX2-": 13,
    "USB3_RX2+": 14,
    "USB3_RX2-": 15,
    # PCIe Gen1 pairs (2 pairs = 4 nets)
    "PCIE_TX+": 16,
    "PCIE_TX-": 17,
    "PCIE_RX+": 18,
    "PCIE_RX-": 19,
    # MIPI D-PHY (2 lanes = 4 nets)
    "MIPI_CLK+": 20,
    "MIPI_CLK-": 21,
    "MIPI_D0+": 22,
    "MIPI_D0-": 23,
    # Single-ended sideband
    "USB_CC1": 24,
    "USB_CC2": 25,
    "MIPI_RST": 26,
}


# =============================================================================
# Diff-pair partner table (consumed by generate_design.py to build the
# NetClassRouting map).  Each pair's positive net maps to the partner
# negative net name.  One-sided declaration is sufficient for the
# router's diff-pair detector (#2558).
# =============================================================================
DIFFPAIRS: dict[str, str] = {
    "USB2_D+": "USB2_D-",
    "USB3_TX1+": "USB3_TX1-",
    "USB3_RX1+": "USB3_RX1-",
    "USB3_TX2+": "USB3_TX2-",
    "USB3_RX2+": "USB3_RX2-",
    "PCIE_TX+": "PCIE_TX-",
    "PCIE_RX+": "PCIE_RX-",
    "MIPI_CLK+": "MIPI_CLK-",
    "MIPI_D0+": "MIPI_D0-",
}


def generate_header() -> str:
    """Generate the PCB file header with 4-layer stackup.

    KiCad's canonical 4-layer numbering is:
        (0  "F.Cu"   signal)
        (1  "In1.Cu" signal)   <- GND plane
        (2  "In2.Cu" signal)   <- PWR plane (split for multiple rails)
        (31 "B.Cu"   signal)

    Reference: tests/fixtures/projects/multilayer_zones.kicad_pcb
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
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
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
    """Emit ``(net N "name")`` lines for every declared net."""
    lines = ['  (net 0 "")']
    for name, num in NETS.items():
        if num > 0:
            lines.append(f'  (net {num} "{name}")')
    return "\n".join(lines)


def generate_board_outline() -> str:
    """Emit the Edge.Cuts rectangle that bounds the board."""
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


# =============================================================================
# Footprint Generators
# =============================================================================
# All sink-side footprints are placed on F.Cu so all routing happens on the
# outer signal layers.  Pad pitch is 0.5mm uniformly (well above the JLCPCB
# tier-1 0.15mm trace/space floor) so within-pair clearances of 0.075mm
# through 0.10mm leave headroom even on the densest pin pairs.
#
# Pin assignment is chosen so that paired pins (P/N) are physically adjacent
# on the sink IC.  This is the realistic case for HSDI footprints (USB 3.0
# PHYs always emit P/N on adjacent BGA balls; PCIe and MIPI follow the same
# convention) and is required for ``coupled_routing`` to engage cleanly
# without escape-routing crossings (#2527).


def _emit_smd_pad(pin: str, x: float, y: float, w: float, h: float, net_name: str) -> str:
    """Emit a single SMD rectangle pad with the given net assignment."""
    net_num = NETS.get(net_name, 0)
    net_str = f'(net {net_num} "{net_name}")' if net_name else ""
    return (
        f'    (pad "{pin}" smd rect (at {x:.3f} {y:.3f}) '
        f'(size {w:.3f} {h:.3f}) (layers "F.Cu" "F.Paste" "F.Mask") {net_str})'
    )


def _emit_through_hole_pad(
    pin: str, x: float, y: float, size: float, drill: float, net_name: str
) -> str:
    """Emit a single through-hole circular pad."""
    net_num = NETS.get(net_name, 0)
    net_str = f'(net {net_num} "{net_name}")' if net_name else ""
    return (
        f'    (pad "{pin}" thru_hole circle (at {x:.3f} {y:.3f}) '
        f"(size {size:.3f} {size:.3f}) (drill {drill:.3f}) "
        f'(layers "*.Cu" "*.Mask") {net_str})'
    )


def generate_usb_c_source() -> str:
    """USB-C receptacle exposing USB 2.0 D+/D- + USB 3.0 SS lanes + CC pins.

    Placed at the top edge of the board.  Pin assignment follows the
    USB-C spec (A side / B side mirrored).  The receptacle is the *source*
    end of the USB 2.0 and USB 3.0 diff pairs.
    """
    # USB-C J1 at top-left of board (centered horizontally over the
    # 12-pin pad array width).  11mm pad span + 2 shield tabs.
    x = BOARD_ORIGIN_X + 15
    y = BOARD_ORIGIN_Y + 8

    # USB-C 24-pin pinout — combines USB 2.0 D+/D- (A6/A7, B6/B7) with
    # USB 3.0 SS lanes (TX1+/-, RX1+/-, TX2+/-, RX2+/-).  Pad pitch is
    # widened to 1.0mm (vs the spec'd 0.5mm) so the autorouter can route
    # between adjacent pins at JLCPCB tier-1 (0.15mm trace + 0.15mm space
    # = 0.45mm channel fits in the 0.7mm gap between 0.3mm-wide pads).
    # This is a routing testbench, not a manufacturable USB-C connector
    # part --- so the deviation from spec pitch is acceptable.
    PITCH = 1.0
    pins = [
        # Pin, x_offset (column index), y_offset, net
        # A row (top-side) — laid out left-to-right at PITCH spacing
        ("A1", 0, 0.0, "GND"),
        ("A2", 1, 0.0, "USB3_TX1+"),
        ("A3", 2, 0.0, "USB3_TX1-"),
        ("A4", 3, 0.0, "VBUS_USB"),
        ("A5", 4, 0.0, "USB_CC1"),
        ("A6", 5, 0.0, "USB2_D+"),
        ("A7", 6, 0.0, "USB2_D-"),
        ("A8", 7, 0.0, "+3V3"),  # SBU1 mapped to a quiet rail
        ("A9", 8, 0.0, "VBUS_USB"),
        ("A10", 9, 0.0, "USB3_RX2-"),
        ("A11", 10, 0.0, "USB3_RX2+"),
        ("A12", 11, 0.0, "GND"),
        # B row (bottom-side) — mirrored x positions, +2.0mm in y
        ("B1", 11, 2.0, "GND"),
        ("B2", 10, 2.0, "USB3_TX2+"),
        ("B3", 9, 2.0, "USB3_TX2-"),
        ("B4", 8, 2.0, "VBUS_USB"),
        ("B5", 7, 2.0, "USB_CC2"),
        ("B6", 6, 2.0, "USB2_D+"),
        ("B7", 5, 2.0, "USB2_D-"),
        ("B8", 4, 2.0, "+3V3"),  # SBU2 mapped to a quiet rail
        ("B9", 3, 2.0, "VBUS_USB"),
        ("B10", 2, 2.0, "USB3_RX1-"),
        ("B11", 1, 2.0, "USB3_RX1+"),
        ("B12", 0, 2.0, "GND"),
    ]

    pads = [
        # Pad 0.3 x 0.6 at PITCH=1.0 -> 0.7mm horizontal gap between
        # adjacent pads, room for 0.15 clearance + 0.15 trace + 0.15
        # clearance = 0.45mm channel.  Row spacing 2.0mm with 0.6mm
        # pad height -> 1.4mm vertical gap (ample).
        _emit_smd_pad(pin, (col - 5.5) * PITCH, py, 0.3, 0.6, net_name)
        for pin, col, py, net_name in pins
    ]

    # Shield/mounting tabs
    pads.append(_emit_through_hole_pad("S1", -7.5, 1.0, 1.0, 0.6, "GND"))
    pads.append(_emit_through_hole_pad("S2", 7.5, 1.0, 1.0, 0.6, "GND"))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_USB:USB_C_Receptacle_USB2.0"
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


def generate_qfn32_usb2_sink() -> str:
    """QFN-32 0.5mm pitch — USB 2.0 sink.

    Sink for the USB 2.0 D+/D- pair.  Pins 1-8 face up (toward J1) so the
    USB pair has a short escape from J1 down to U1.  All other pins tied
    to GND or VCC (this is a synthetic sink, not a real PHY).
    """
    # QFN-32 USB 2.0 sink: directly below J1 with adequate channel
    # space (~5mm) for the USB 2.0 D+/D- pair escape from J1.A6/A7.
    x = BOARD_ORIGIN_X + 15
    y = BOARD_ORIGIN_Y + 22

    # QFN-32, 0.8mm pitch (widened from real 0.5mm spec for routing
    # feasibility at JLCPCB tier-1), 7mm body.  8 pins per side.
    # Pin layout (counter-clockwise starting bottom-left):
    #   pins 1-8   on left side (bottom to top)
    #   pins 9-16  on top side (left to right)     <- USB 2.0 pair here (10/11)
    #   pins 17-24 on right side (top to bottom)
    #   pins 25-32 on bottom (right to left)
    pitch = 0.8
    pad_offset = 3.5  # body half-width
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 3.5) * pitch

    # Pin -> net mapping.  Diff pair on top edge (pins 10/11 adjacent).
    pin_nets: dict[int, str] = dict.fromkeys(range(1, 33), "GND")
    pin_nets[10] = "USB2_D+"
    pin_nets[11] = "USB2_D-"
    # Pin 12/13 host the USB_CC1/CC2 sideband on the sink side so they
    # are not single-pad nets (which would fire the ``single_pad_net``
    # DRC rule).  In a real device the CC pins would route to a USB-C
    # configuration controller; for the test bench we synthesize that
    # endpoint on U1 to give the autorouter something to terminate to.
    pin_nets[12] = "USB_CC1"
    pin_nets[13] = "USB_CC2"
    # Power pins
    pin_nets[1] = "+3V3"
    pin_nets[17] = "+3V3"
    pin_nets[9] = "VBUS_USB"
    pin_nets[16] = "+1V8"

    pads: list[str] = []

    # Left side (pins 1-8, bottom-to-top)
    for i in range(8):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))

    # Top side (pins 9-16, left-to-right)
    for i in range(8):
        pin = i + 9
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    # Right side (pins 17-24, top-to-bottom)
    for i in range(8):
        pin = i + 17
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))

    # Bottom side (pins 25-32, right-to-left)
    for i in range(8):
        pin = i + 25
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U1" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "QFN32_USB2" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_bga49_usb3_sink() -> str:
    """BGA-49 simulator — USB 3.0 SuperSpeed sink (4 pairs).

    7x7 grid of pads at 0.5mm pitch (a "BGA escape simulator" — we use SMD
    pads instead of real BGA balls because this is for routing exercise,
    not assembly).  USB 3.0 pairs land on adjacent grid positions to
    force the router into the Phase 2F BGA-escape coupling path.
    """
    # BGA-49 USB 3.0 sink: top-right quadrant.  4 SS lanes (8 nets)
    # escape out the four sides of the BGA, then turn back to J1's
    # USB 3.0 pads.  At 1.27mm pitch, BGA spans 7.62mm (7 pads).
    x = BOARD_ORIGIN_X + 55
    y = BOARD_ORIGIN_Y + 20

    # BGA pitch 1.27mm (50 mil): real BGA-49 packages use 0.5mm or
    # 0.4mm pitch, which requires micro-vias and JLCPCB tier-2 process.
    # This is a routing testbench at tier-1, so we widen to 1.27mm
    # pitch with 0.45mm pads -> 0.82mm gap.  At tier-1 (0.15mm clearance,
    # 0.15mm trace, 0.45mm via diameter), an escape via centered between
    # adjacent pads has 0.49mm from pad center; via outer edge sits
    # 0.265mm from pad center -> 0.04mm headroom above 0.15mm clearance.
    pitch = 1.27
    pad_size = 0.45

    # 7x7 = 49 pads, addressed as (row, col) -> "RowLetter+ColNumber"
    # e.g. "A1" = top-left.  Diff pair pads are on row B and row F (one row in
    # from the perimeter) so the router has to cross the outer ring of
    # GND pads via escape coupling.
    pin_nets: dict[str, str] = {}

    # Default every pad to GND, then override with specific nets.  This
    # keeps the perimeter ring and any unused inner pads tied to the
    # GND plane reference and prevents KeyError on inner pads we don't
    # explicitly assign (B4 / B7 / F4 / F7 etc.).
    for row_letter in "ABCDEFG":
        for col in range(1, 8):
            pin_nets[f"{row_letter}{col}"] = "GND"

    # Inner 5x5 (rows C/D/E, cols 2-6): power rails as a power-domain
    # reference plane.
    for row in "CDE":
        for col in range(2, 7):
            pin_nets[f"{row}{col}"] = "+1V2"
    pin_nets["C2"] = "+3V3"
    pin_nets["C6"] = "+3V3"
    pin_nets["E2"] = "+3V3"
    pin_nets["E6"] = "+3V3"

    # USB 3.0 pairs on row B and row F (adjacent P/N columns).
    # Within each row: cols 2/3 carry one pair, cols 5/6 carry the other.
    # B2/B3 -> TX1+/TX1-, B5/B6 -> RX1+/RX1-
    pin_nets["B2"] = "USB3_TX1+"
    pin_nets["B3"] = "USB3_TX1-"
    pin_nets["B5"] = "USB3_RX1+"
    pin_nets["B6"] = "USB3_RX1-"
    # F2/F3 -> TX2+/TX2-, F5/F6 -> RX2+/RX2-
    pin_nets["F2"] = "USB3_TX2+"
    pin_nets["F3"] = "USB3_TX2-"
    pin_nets["F5"] = "USB3_RX2+"
    pin_nets["F6"] = "USB3_RX2-"

    pads: list[str] = []
    for row_idx, row_letter in enumerate("ABCDEFG"):
        for col in range(1, 8):
            pin = f"{row_letter}{col}"
            px = (col - 4) * pitch  # col 4 is center -> -1.5 .. +1.5
            py = (row_idx - 3) * pitch  # row D is center
            pads.append(_emit_smd_pad(pin, px, py, pad_size, pad_size, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U2" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "BGA49_USB3" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_mini_pcie_source() -> str:
    """Mini-PCIe edge connector (synthetic castellated pad row) — PCIe source.

    52-pin card-edge connector at 0.8mm pitch.  We model it as a row of
    castellated pads along the right edge of the board so the PCIe
    differential pairs route inward to U3 (QFP-48 sink) with adequate
    length to exercise serpentine insertion (Phase 3I).
    """
    # Mini-PCIe edge: right edge of board.  Pads point inward toward U3.
    x = BOARD_ORIGIN_X + 95  # near right edge
    y = BOARD_ORIGIN_Y + 45  # mid-height

    pitch = 0.8
    pad_w = 0.5
    pad_h = 0.6  # 0.6 height at 0.8 pitch = 0.2mm vertical gap, safe at JLCPCB tier-1

    # Sparse pin assignment — only the 4 PCIe pins + GND straps need real
    # nets, the rest are NC.
    pcie_pins: list[tuple[str, float, str]] = []
    for i in range(12):
        py = (i - 5.5) * pitch
        # Pins 4-7 carry the PCIe pairs (adjacent)
        if i == 4:
            net = "PCIE_TX+"
        elif i == 5:
            net = "PCIE_TX-"
        elif i == 7:
            net = "PCIE_RX+"
        elif i == 8:
            net = "PCIE_RX-"
        elif i in (0, 1, 10, 11):
            net = "GND"
        elif i == 2:
            net = "+3V3"
        elif i == 9:
            net = "+1V2"
        else:
            net = "GND"
        pcie_pins.append((str(i + 1), py, net))

    pads = [_emit_smd_pad(pin, 0.0, py, pad_w, pad_h, net) for pin, py, net in pcie_pins]

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_PCIE:PCIE_Mini_Edge"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J3" (at 0 -7) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "MiniPCIe" (at 0 7) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_qfp48_pcie_sink() -> str:
    """QFP-48 0.5mm pitch — PCIe sink.

    Synthetic QFP-48 receiving the PCIe Gen1 TX/RX pairs from J3.  12 pins
    per side at 0.5mm pitch.  Diff pairs land on adjacent left-side pins
    so the router has to bend the trace pair around the package corner —
    this is what triggers length skew (PCIe Gen1 spec: <0.5mm skew).
    """
    # QFP-48 PCIe sink: mid-right.  Right side (pins 25-36) faces J3.
    x = BOARD_ORIGIN_X + 70
    y = BOARD_ORIGIN_Y + 45

    # QFP-48 widened from 0.5mm to 0.8mm pitch (12 pins/side -> 9.6mm span,
    # so body becomes 10mm).  Real LQFP-48 is 0.5mm at JLCPCB tier-1; we
    # relax for routing feasibility.
    pitch = 0.8
    pad_offset = 5.0  # 10mm body half-width
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 5.5) * pitch  # 12 pins per side -> -4.4..+4.4

    # Default all GND
    pin_nets: dict[int, str] = dict.fromkeys(range(1, 49), "GND")
    # Right side (closest to J3) carries the PCIe pairs.
    # Right side pins are 25-36 (12 pins).
    pin_nets[28] = "PCIE_TX+"
    pin_nets[29] = "PCIE_TX-"
    pin_nets[31] = "PCIE_RX+"
    pin_nets[32] = "PCIE_RX-"
    pin_nets[25] = "+3V3"
    pin_nets[36] = "+1V2"

    pads: list[str] = []
    # Left (1-12, bottom-to-top)
    for i in range(12):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Top (13-24, left-to-right)
    for i in range(12):
        pin = i + 13
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    # Right (25-36, top-to-bottom)
    for i in range(12):
        pin = i + 25
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Bottom (37-48, right-to-left)
    for i in range(12):
        pin = i + 37
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U3" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "QFP48_PCIe" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_ffc_mipi_source() -> str:
    """4-pin FFC connector — MIPI source.

    Tiny 4-pin FFC connector at 0.5mm pitch sourcing the MIPI CLK and D0
    differential pairs.  Note: 4 pins for 4 nets (CLK+/CLK-/D0+/D0-) is
    minimal; a real MIPI source would have more pins for power and reset.
    """
    # FFC MIPI source: bottom-left.
    x = BOARD_ORIGIN_X + 8
    y = BOARD_ORIGIN_Y + 70

    # FFC widened to 0.8mm pitch (real FFC connectors at 0.5mm pitch
    # require thin pads + slot escape; we use 0.8mm for tier-1 fitness).
    pitch = 0.8
    pad_w = 0.3
    pad_h = 0.8

    pins = [
        ("1", "MIPI_CLK+"),
        ("2", "MIPI_CLK-"),
        ("3", "MIPI_D0+"),
        ("4", "MIPI_D0-"),
    ]

    pads = [
        _emit_smd_pad(pin, (i - 1.5) * pitch, 0.0, pad_w, pad_h, net)
        for i, (pin, net) in enumerate(pins)
    ]
    # Mounting GND tabs
    pads.append(_emit_through_hole_pad("M1", -1.5, 1.2, 1.0, 0.6, "GND"))
    pads.append(_emit_through_hole_pad("M2", 1.5, 1.2, 1.0, 0.6, "GND"))
    # Reset side-channel pad
    pads.append(_emit_smd_pad("RST", 0.0, -1.5, 0.5, 0.5, "MIPI_RST"))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_FFC:FFC_4P_0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J4" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "FFC4" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


def generate_qfn24_mipi_sink() -> str:
    """QFN-24 0.5mm pitch — MIPI sink.

    6 pins per side at 0.5mm pitch.  MIPI pairs land on the left side
    (pins 1-6, adjacent to J4 on the board's left edge) so the diff
    pair has a short, mostly-straight escape that exercises Phase 3K
    impedance + Phase 3I serpentine to hit the tight MIPI skew target.
    """
    # QFN-24 MIPI sink: just right of J4 (FFC).  Left pins (1-6) face J4.
    x = BOARD_ORIGIN_X + 30
    y = BOARD_ORIGIN_Y + 70

    # QFN-24 widened from 0.5mm to 0.8mm pitch.  6 pins/side at 0.8mm
    # pitch -> 4.0mm span, body becomes 6mm.
    pitch = 0.8
    pad_offset = 3.0  # 6mm body half-width
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 2.5) * pitch  # 6 pins per side -> -2.0 .. +2.0

    pin_nets: dict[int, str] = dict.fromkeys(range(1, 25), "GND")
    pin_nets[1] = "MIPI_CLK+"
    pin_nets[2] = "MIPI_CLK-"
    pin_nets[3] = "MIPI_D0+"
    pin_nets[4] = "MIPI_D0-"
    pin_nets[5] = "MIPI_RST"
    pin_nets[6] = "+1V8"
    pin_nets[12] = "+3V3"
    pin_nets[18] = "+1V8"

    pads: list[str] = []
    # Left (1-6)
    for i in range(6):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Top (7-12)
    for i in range(6):
        pin = i + 7
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    # Right (13-18)
    for i in range(6):
        pin = i + 13
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Bottom (19-24)
    for i in range(6):
        pin = i + 19
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U4" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "QFN24_MIPI" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


# =============================================================================
# Top-level PCB Generator
# =============================================================================


def generate_pcb() -> str:
    """Generate the complete PCB file."""
    parts = [
        generate_header(),
        generate_nets(),
        generate_board_outline(),
        # Source footprints
        generate_usb_c_source(),
        generate_mini_pcie_source(),
        generate_ffc_mipi_source(),
        # Sink footprints
        generate_qfn32_usb2_sink(),
        generate_bga49_usb3_sink(),
        generate_qfp48_pcie_sink(),
        generate_qfn24_mipi_sink(),
    ]
    parts.append(")")  # close kicad_pcb
    return "\n".join(parts)


def main() -> int:
    """Generate the PCB file."""
    output_file = sys.argv[1] if len(sys.argv) > 1 else "output/diffpair_test.kicad_pcb"
    output_path = Path(output_file)
    if not output_path.is_absolute():
        output_path = Path(__file__).parent / output_file

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("  Stackup: 4-layer (F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu)")
    print("  Components: 1 USB-C, 1 mini-PCIe edge, 1 FFC, 1 QFN-32, 1 BGA-49, 1 QFP-48, 1 QFN-24")
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])}")
    print(f"  Diff pairs: {len(DIFFPAIRS)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
