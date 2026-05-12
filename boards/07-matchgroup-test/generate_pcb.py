#!/usr/bin/env python3
"""
Generate a KiCad PCB for the match-group test board (board 07).

This script creates a 4-layer PCB file (F.Cu / In1.Cu / In2.Cu / B.Cu)
demonstrating each match-group scenario of Epic #2661 Phase 3L:

- DDR data byte (10 nets: DQ0-7 + DM0 + DQS_P/N pair) -- N-trace group
  + diff pair member (Phase 2F group-of-pairs composition)
- MIPI CSI lane group (3 pairs = 6 nets: CLK + DAT0 + DAT1)
- HDMI TMDS lane group (3 pairs = 6 nets: D0 + D1 + D2)
- Generic address bus (8 nets: A0-A7) -- single-ended N-trace group

Net assignment follows board 06's convention (NETS dict + per-pad
``(net N "name")`` s-expr emission).  The 4-layer stackup (F.Cu /
In1.Cu GND / In2.Cu PWR / B.Cu) is identical to board 06 so the same
JLCPCB tier-1 stackup the Phase 3K impedance formulas were calibrated
against drives both boards.

Usage:
    python generate_pcb.py [output_file]
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path


def generate_uuid() -> str:
    """Generate a KiCad-format UUID."""
    return str(uuid.uuid4())


# Board dimensions (mm) -- generous size keeps source/sink pairs well
# separated so the autorouter has plenty of channel space for each
# match group to escape and meander.
BOARD_WIDTH = 110.0
BOARD_HEIGHT = 95.0
BOARD_ORIGIN_X = 100.0
BOARD_ORIGIN_Y = 100.0

# =============================================================================
# Net Definitions
# =============================================================================
# Net assignment follows board 06 convention: dict[name -> int].
# Net 0 is the implicit unconnected net.  ~33 signal nets total:
#   - 10 DDR data byte (DQ0-7 + DM0 + DQS pair)
#   - 6 MIPI CSI lanes (3 pairs)
#   - 6 HDMI TMDS lanes (3 pairs)
#   - 8 ADDR bus (A0-A7)
#   - 3 power rails (+1V2, +1V8, GND)
NETS: dict[str, int] = {
    "": 0,
    # Power rails
    "+1V2": 1,
    "+1V8": 2,
    "GND": 3,
    # DDR data byte 0: DQ0-7 + DM0 (single-ended) + DQS_P/N (diff pair)
    "DQ0": 4,
    "DQ1": 5,
    "DQ2": 6,
    "DQ3": 7,
    "DQ4": 8,
    "DQ5": 9,
    "DQ6": 10,
    "DQ7": 11,
    "DM0": 12,
    "DQS_P": 13,
    "DQS_N": 14,
    # MIPI CSI: CLK + DAT0 + DAT1 (3 pairs)
    "MIPI_CLK_P": 15,
    "MIPI_CLK_N": 16,
    "MIPI_DAT0_P": 17,
    "MIPI_DAT0_N": 18,
    "MIPI_DAT1_P": 19,
    "MIPI_DAT1_N": 20,
    # HDMI TMDS: D0 + D1 + D2 (3 pairs; clock excluded per scope)
    "TMDS_D0_P": 21,
    "TMDS_D0_N": 22,
    "TMDS_D1_P": 23,
    "TMDS_D1_N": 24,
    "TMDS_D2_P": 25,
    "TMDS_D2_N": 26,
    # ADDR bus A0-A7 (single-ended parallel bus)
    "A0": 27,
    "A1": 28,
    "A2": 29,
    "A3": 30,
    "A4": 31,
    "A5": 32,
    "A6": 33,
    "A7": 34,
}


# =============================================================================
# Match-group declarations (consumed by generate_design.py to build the
# NetClassRouting map and the MatchGroupTracker entries).
# =============================================================================
# DDR data byte: 9 single-ended nets + 1 diff pair (DQS).  This is the
# Phase 2F group-of-pairs composition exercise: a group whose members
# include both single-ended nets AND a differential pair.  Phase 1A
# declares it via length_match_group on the NetClassRouting; Phase 1C
# detection (#2689) groups them; Phase 2F (#2701) tunes them as a
# composite group with symmetric serpentine geometry on DQS.
DDR_DATA_BYTE_0_SINGLES: list[str] = [
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
    "DM0",
]
DDR_DATA_BYTE_0_PAIRS: list[tuple[str, str]] = [
    ("DQS_P", "DQS_N"),
]

# MIPI CSI: 3 pairs forming one length-matched group.  Phase 2F
# group-of-pairs composition.  No single-ended members.
MIPI_CSI_LANES_PAIRS: list[tuple[str, str]] = [
    ("MIPI_CLK_P", "MIPI_CLK_N"),
    ("MIPI_DAT0_P", "MIPI_DAT0_N"),
    ("MIPI_DAT1_P", "MIPI_DAT1_N"),
]

# HDMI TMDS: 3 pairs forming one length-matched group.  Phase 2F
# group-of-pairs composition.  Clock pair excluded per issue scope --
# in real designs the lanes match to the clock pair externally; for
# this testbench all 3 lanes match to each other.
HDMI_TMDS_LANES_PAIRS: list[tuple[str, str]] = [
    ("TMDS_D0_P", "TMDS_D0_N"),
    ("TMDS_D1_P", "TMDS_D1_N"),
    ("TMDS_D2_P", "TMDS_D2_N"),
]

# Address bus A0-A7: pure single-ended N-trace group with looser
# tolerance (parallel-bus commodity tier).  Phase 1A declaration +
# Phase 1C suffix-inference fallback (opt-in).
ADDR_BUS_SINGLES: list[str] = ["A0", "A1", "A2", "A3", "A4", "A5", "A6", "A7"]

# Diff-pair partner table (consumed by generate_design.py for diffpair
# annotation).  The DDR DQS pair lives inside the DDR_DATA_BYTE_0
# match group; the MIPI/HDMI pairs constitute the MIPI_CSI_LANES /
# HDMI_TMDS_LANES groups respectively.  One-sided declaration
# sufficient for the router's diff-pair detector (#2558).
DIFFPAIRS: dict[str, str] = {
    # DDR strobe (member of DDR_DATA_BYTE_0 group)
    "DQS_P": "DQS_N",
    # MIPI CSI lanes
    "MIPI_CLK_P": "MIPI_CLK_N",
    "MIPI_DAT0_P": "MIPI_DAT0_N",
    "MIPI_DAT1_P": "MIPI_DAT1_N",
    # HDMI TMDS lanes
    "TMDS_D0_P": "TMDS_D0_N",
    "TMDS_D1_P": "TMDS_D1_N",
    "TMDS_D2_P": "TMDS_D2_N",
}


def generate_header() -> str:
    """Generate the PCB file header with 4-layer stackup.

    KiCad 10 format (PR #2716).  Identical layer stackup to board 06:
        (0  "F.Cu"   signal)
        (1  "In1.Cu" signal)   <- GND plane
        (2  "In2.Cu" signal)   <- PWR plane (+1V2 / +1V8)
        (31 "B.Cu"   signal)
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
# outer signal layers.  Pad pitch widened to 0.8mm uniformly (above the
# JLCPCB tier-1 0.15mm trace/space floor, mirroring board 06's tier-1
# fitness widening).
#
# Pin assignment is chosen so paired members of each match group are
# physically clustered on the sink footprint, giving the autorouter a
# compact escape pattern but realistic enough escape lengths to expose
# group-skew differences post-route.


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


def generate_qfn48_ddr_controller() -> str:
    """QFN-48 0.8mm pitch -- DDR controller (source side of DDR data byte).

    12 pins per side at 0.8mm pitch -- 9.6mm span, 11mm body.  DDR
    data-byte signals (DQ0-7 + DM0 + DQS_P/N) are clustered on the
    right side (pins 25-36) facing U2 (the DRAM sink) so the byte has
    a clean escape across the routing channel.
    """
    x = BOARD_ORIGIN_X + 20
    y = BOARD_ORIGIN_Y + 25

    pitch = 0.8
    pad_offset = 5.0
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 5.5) * pitch  # 12 pins per side -> -4.4..+4.4

    pin_nets: dict[int, str] = dict.fromkeys(range(1, 49), "GND")
    # Right side (25-36) carries the DDR byte: DQ0-DQ7 + DM0 + DQS_P + DQS_N
    pin_nets[25] = "DQ0"
    pin_nets[26] = "DQ1"
    pin_nets[27] = "DQ2"
    pin_nets[28] = "DQ3"
    pin_nets[29] = "DM0"
    pin_nets[30] = "DQS_P"
    pin_nets[31] = "DQS_N"
    pin_nets[32] = "DQ4"
    pin_nets[33] = "DQ5"
    pin_nets[34] = "DQ6"
    pin_nets[35] = "DQ7"
    pin_nets[36] = "+1V2"
    # Power on left side
    pin_nets[1] = "+1V8"
    pin_nets[12] = "+1V2"

    pads: list[str] = []
    # Left (1-12)
    for i in range(12):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Top (13-24)
    for i in range(12):
        pin = i + 13
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    # Right (25-36)
    for i in range(12):
        pin = i + 25
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    # Bottom (37-48)
    for i in range(12):
        pin = i + 37
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U1" (at 0 -6) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "QFN48_DDR_CTRL" (at 0 6) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_qfn48_ddr_sink() -> str:
    """QFN-48 0.8mm pitch -- DDR DRAM sink (DDR data byte termination).

    Mirror of U1 placed across the routing channel.  Left side (pins
    1-12) faces U1 and carries the DDR byte signals.
    """
    x = BOARD_ORIGIN_X + 50
    y = BOARD_ORIGIN_Y + 25

    pitch = 0.8
    pad_offset = 5.0
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 5.5) * pitch

    pin_nets: dict[int, str] = dict.fromkeys(range(1, 49), "GND")
    # Left side (1-12) carries the DDR byte (mirror of U1's right side)
    pin_nets[1] = "DQ0"
    pin_nets[2] = "DQ1"
    pin_nets[3] = "DQ2"
    pin_nets[4] = "DQ3"
    pin_nets[5] = "DM0"
    pin_nets[6] = "DQS_P"
    pin_nets[7] = "DQS_N"
    pin_nets[8] = "DQ4"
    pin_nets[9] = "DQ5"
    pin_nets[10] = "DQ6"
    pin_nets[11] = "DQ7"
    pin_nets[12] = "+1V2"
    # Power
    pin_nets[24] = "+1V2"
    pin_nets[36] = "+1V8"

    pads: list[str] = []
    for i in range(12):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(12):
        pin = i + 13
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    for i in range(12):
        pin = i + 25
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(12):
        pin = i + 37
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U2" (at 0 -6) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "QFN48_DRAM" (at 0 6) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_ffc_mipi_source() -> str:
    """4-pin FFC connector source -- MIPI CSI source.

    Wider FFC than board 06 to accommodate 6 pins (3 pairs).  At 1.0mm
    pitch the 6-pin span is 5mm.
    """
    x = BOARD_ORIGIN_X + 15
    y = BOARD_ORIGIN_Y + 55

    pitch = 1.0
    pad_w = 0.4
    pad_h = 0.9

    pins = [
        ("1", "MIPI_CLK_P"),
        ("2", "MIPI_CLK_N"),
        ("3", "MIPI_DAT0_P"),
        ("4", "MIPI_DAT0_N"),
        ("5", "MIPI_DAT1_P"),
        ("6", "MIPI_DAT1_N"),
    ]

    pads = [
        _emit_smd_pad(pin, (i - 2.5) * pitch, 0.0, pad_w, pad_h, net)
        for i, (pin, net) in enumerate(pins)
    ]
    pads.append(_emit_through_hole_pad("M1", -3.5, 1.5, 1.0, 0.6, "GND"))
    pads.append(_emit_through_hole_pad("M2", 3.5, 1.5, 1.0, 0.6, "GND"))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_FFC:FFC_6P_1.0mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J1" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "FFC6_MIPI" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


def generate_qfn24_mipi_sink() -> str:
    """QFN-24 0.8mm pitch -- MIPI CSI sink (3 pairs).

    Left side (pins 1-6) faces J1 with the 3 pairs landing on adjacent
    pins for clean P/N escape.
    """
    x = BOARD_ORIGIN_X + 35
    y = BOARD_ORIGIN_Y + 55

    pitch = 0.8
    pad_offset = 3.0
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 2.5) * pitch  # 6 pins per side

    pin_nets: dict[int, str] = dict.fromkeys(range(1, 25), "GND")
    pin_nets[1] = "MIPI_CLK_P"
    pin_nets[2] = "MIPI_CLK_N"
    pin_nets[3] = "MIPI_DAT0_P"
    pin_nets[4] = "MIPI_DAT0_N"
    pin_nets[5] = "MIPI_DAT1_P"
    pin_nets[6] = "MIPI_DAT1_N"
    pin_nets[12] = "+1V8"
    pin_nets[18] = "+1V2"

    pads: list[str] = []
    for i in range(6):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(6):
        pin = i + 7
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    for i in range(6):
        pin = i + 13
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(6):
        pin = i + 19
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U3" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "QFN24_MIPI" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


def generate_hdmi_connector() -> str:
    """HDMI receptacle (synthetic 19-pin) -- HDMI source.

    Synthetic shrouded HDMI footprint at 1.0mm pitch.  Sources only
    the 6 TMDS lane nets plus shield/GND straps (the clock pair and
    side channels are NC for this testbench).
    """
    x = BOARD_ORIGIN_X + 60
    y = BOARD_ORIGIN_Y + 55

    pitch = 1.0
    pad_w = 0.4
    pad_h = 1.0

    # 6 TMDS pins + GND + shield tabs.  Pin layout: TMDS pairs adjacent.
    pins = [
        ("1", "TMDS_D0_P"),
        ("2", "TMDS_D0_N"),
        ("3", "GND"),
        ("4", "TMDS_D1_P"),
        ("5", "TMDS_D1_N"),
        ("6", "GND"),
        ("7", "TMDS_D2_P"),
        ("8", "TMDS_D2_N"),
    ]

    pads = [
        _emit_smd_pad(pin, (i - 3.5) * pitch, 0.0, pad_w, pad_h, net)
        for i, (pin, net) in enumerate(pins)
    ]
    pads.append(_emit_through_hole_pad("S1", -5.0, 1.5, 1.2, 0.7, "GND"))
    pads.append(_emit_through_hole_pad("S2", 5.0, 1.5, 1.2, 0.7, "GND"))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_Video:HDMI_A_Receptacle"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J2" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "HDMI19" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


def generate_bga49_hdmi_sink() -> str:
    """BGA-49 simulator -- HDMI TMDS sink (3 pairs).

    7x7 SMD pad grid at 1.27mm pitch.  TMDS pairs on row B with
    adjacent column placement so the router has to escape the BGA's
    perimeter ring before turning toward J2.
    """
    x = BOARD_ORIGIN_X + 85
    y = BOARD_ORIGIN_Y + 55

    pitch = 1.27
    pad_size = 0.45

    pin_nets: dict[str, str] = {}
    for row_letter in "ABCDEFG":
        for col in range(1, 8):
            pin_nets[f"{row_letter}{col}"] = "GND"

    # Inner 5x5 (rows C/D/E, cols 2-6): power rails as a power-domain ref.
    for row in "CDE":
        for col in range(2, 7):
            pin_nets[f"{row}{col}"] = "+1V8"
    pin_nets["C2"] = "+1V2"
    pin_nets["E6"] = "+1V2"

    # TMDS pairs on row B (3 pairs across cols 1/2, 3/4, 5/6).
    pin_nets["B1"] = "TMDS_D0_P"
    pin_nets["B2"] = "TMDS_D0_N"
    pin_nets["B3"] = "TMDS_D1_P"
    pin_nets["B4"] = "TMDS_D1_N"
    pin_nets["B5"] = "TMDS_D2_P"
    pin_nets["B6"] = "TMDS_D2_N"

    pads: list[str] = []
    for row_idx, row_letter in enumerate("ABCDEFG"):
        for col in range(1, 8):
            pin = f"{row_letter}{col}"
            px = (col - 4) * pitch
            py = (row_idx - 3) * pitch
            pads.append(_emit_smd_pad(pin, px, py, pad_size, pad_size, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U4" (at 0 -4) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "BGA49_HDMI" (at 0 4) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
{pads_str}
  )"""


def generate_addr_header() -> str:
    """0.1in pitch 9-pin header -- address bus source.

    Generic FPGA-style header sourcing A0-A7 (8 nets) + GND.  Through-
    hole pads at 2.54mm pitch (0.1in).
    """
    x = BOARD_ORIGIN_X + 20
    y = BOARD_ORIGIN_Y + 80

    pitch = 2.54
    pad_size = 1.5
    drill = 0.8

    # 9 pins: A0..A7 + GND
    pins = [
        ("1", "GND"),
        ("2", "A0"),
        ("3", "A1"),
        ("4", "A2"),
        ("5", "A3"),
        ("6", "A4"),
        ("7", "A5"),
        ("8", "A6"),
        ("9", "A7"),
    ]

    pads = [
        _emit_through_hole_pad(pin, (i - 4) * pitch, 0.0, pad_size, drill, net)
        for i, (pin, net) in enumerate(pins)
    ]

    pads_str = "\n".join(pads)
    return f"""  (footprint "Connector_PinHeader_2.54mm:PinHeader_1x09_P2.54mm_Vertical"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "J3" (at 0 -3) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
    (fp_text value "ADDR_HDR" (at 0 3) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 0.8 0.8) (thickness 0.12)))
    )
{pads_str}
  )"""


def generate_qfp48_addr_sink() -> str:
    """QFP-48 0.8mm pitch -- address bus sink (SRAM-style endpoint).

    Left side (pins 1-12) faces J3 and carries A0-A7 on adjacent pins.
    """
    x = BOARD_ORIGIN_X + 60
    y = BOARD_ORIGIN_Y + 80

    pitch = 0.8
    pad_offset = 5.0
    pad_w = 0.3
    pad_h = 0.35

    def pin_offset(i: int) -> float:
        return (i - 5.5) * pitch

    pin_nets: dict[int, str] = dict.fromkeys(range(1, 49), "GND")
    pin_nets[1] = "A0"
    pin_nets[2] = "A1"
    pin_nets[3] = "A2"
    pin_nets[4] = "A3"
    pin_nets[5] = "A4"
    pin_nets[6] = "A5"
    pin_nets[7] = "A6"
    pin_nets[8] = "A7"
    pin_nets[12] = "+1V8"
    pin_nets[24] = "+1V2"
    pin_nets[36] = "+1V8"

    pads: list[str] = []
    for i in range(12):
        pin = i + 1
        py = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), -pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(12):
        pin = i + 13
        px = pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, -pad_offset, pad_w, pad_h * 2, pin_nets[pin]))
    for i in range(12):
        pin = i + 25
        py = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), pad_offset, py, pad_h * 2, pad_w, pin_nets[pin]))
    for i in range(12):
        pin = i + 37
        px = -pin_offset(i)
        pads.append(_emit_smd_pad(str(pin), px, pad_offset, pad_w, pad_h * 2, pin_nets[pin]))

    pads_str = "\n".join(pads)
    return f"""  (footprint "Package_QFP:LQFP-48_7x7mm_P0.5mm"
    (layer "F.Cu")
    (uuid "{generate_uuid()}")
    (at {x} {y})
    (fp_text reference "U5" (at 0 -5) (layer "F.SilkS") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
    )
    (fp_text value "QFP48_SRAM" (at 0 5) (layer "F.Fab") (uuid "{generate_uuid()}")
      (effects (font (size 1 1) (thickness 0.15)))
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
        # DDR data byte
        generate_qfn48_ddr_controller(),
        generate_qfn48_ddr_sink(),
        # MIPI CSI
        generate_ffc_mipi_source(),
        generate_qfn24_mipi_sink(),
        # HDMI TMDS
        generate_hdmi_connector(),
        generate_bga49_hdmi_sink(),
        # Address bus
        generate_addr_header(),
        generate_qfp48_addr_sink(),
    ]
    parts.append(")")  # close kicad_pcb
    return "\n".join(parts)


def main() -> int:
    """Generate the PCB file."""
    output_file = sys.argv[1] if len(sys.argv) > 1 else "output/matchgroup_test.kicad_pcb"
    output_path = Path(output_file)
    if not output_path.is_absolute():
        output_path = Path(__file__).parent / output_file

    output_path.parent.mkdir(parents=True, exist_ok=True)

    pcb_content = generate_pcb()
    output_path.write_text(pcb_content)

    print(f"Generated: {output_path}")
    print(f"  Board size: {BOARD_WIDTH}mm x {BOARD_HEIGHT}mm")
    print("  Stackup: 4-layer (F.Cu / In1.Cu GND / In2.Cu PWR / B.Cu)")
    print(
        "  Components: 2 QFN-48 (DDR), 1 FFC-6 + 1 QFN-24 (MIPI), "
        "1 HDMI + 1 BGA-49 (HDMI), 1 9-pin header + 1 QFP-48 (ADDR)"
    )
    print(f"  Nets: {len([n for n in NETS.values() if n > 0])}")
    print(f"  Diff pairs: {len(DIFFPAIRS)}")
    print(
        f"  Match groups: 4 (DDR_DATA_BYTE_0={len(DDR_DATA_BYTE_0_SINGLES)}+{len(DDR_DATA_BYTE_0_PAIRS)}p, "
        f"MIPI_CSI_LANES={len(MIPI_CSI_LANES_PAIRS)}p, "
        f"HDMI_TMDS_LANES={len(HDMI_TMDS_LANES_PAIRS)}p, "
        f"ADDR_BUS={len(ADDR_BUS_SINGLES)})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
