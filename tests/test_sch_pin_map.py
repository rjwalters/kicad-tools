"""Tests for the sch pin-map command.

Covers net tracing via wire graph, power symbol resolution, --ref filter,
multi-unit symbol merging, unconnected pins, JSON/table output, and CLI smoke test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.sch_pin_map import (
    _build_wire_graph,
    _flood_fill_net,
    _point_on_segment,
    _to_coord,
    main as pin_map_main,
    resolve_pin_map,
)
from kicad_tools.schema import Schematic

# ---------------------------------------------------------------------------
# Minimal schematic with symbols, wires, labels, and a power symbol
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000001")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
    (symbol "Device:C"
      (symbol "C_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 2.794)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 2.794)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "aaaa-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (symbol
    (lib_id "Device:C")
    (at 120 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "bbbb-bbbb")
    (property "Reference" "C1" (at 122 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "100nF" (at 122 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p3"))
    (pin "2" (uuid "p4"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (wire (pts (xy 100 40) (xy 120 40))
    (stroke (width 0) (type default)) (uuid "w2"))
  (wire (pts (xy 120 40) (xy 120 46.19))
    (stroke (width 0) (type default)) (uuid "w3"))
  (wire (pts (xy 100 53.81) (xy 100 60))
    (stroke (width 0) (type default)) (uuid "w4"))
  (wire (pts (xy 100 60) (xy 120 60))
    (stroke (width 0) (type default)) (uuid "w5"))
  (wire (pts (xy 120 60) (xy 120 53.81))
    (stroke (width 0) (type default)) (uuid "w6"))
  (label "VIN" (at 100 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vin"))
  (label "GND" (at 100 60 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-gnd"))
)
"""

# Schematic with a power symbol instead of labels
POWER_SYMBOL_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000002")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "cccc-cccc")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "4.7k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
  (symbol
    (lib_id "power:+3.3V")
    (at 100 40 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "dddd-dddd")
    (property "Reference" "#PWR01" (at 100 36 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "+3.3V" (at 100 36 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "p5"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
)
"""

# Schematic with an unconnected pin
UNCONNECTED_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000003")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "eeee-eeee")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
)
"""


# Schematic with a rotated symbol (90 degrees)
ROTATED_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000004")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 90)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "rot-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
)
"""

# Schematic with a mirrored symbol (mirror x)
MIRRORED_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000005")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:R")
    (at 100 50 0)
    (mirror x)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "mir-aaaa")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1"))
    (pin "2" (uuid "p2"))
  )
)
"""


# ---------------------------------------------------------------------------
# Schematic with diode + resistor where wire path traverses through resistor
# body to a power net.  Without BFS barriers, D1 pin K (cathode) would
# incorrectly resolve to GND by hopping through R1's body.
#
# Topology:
#   D1 (D_Schottky) at (120, 50) rotated 90 degrees
#     Pin 1 (K, cathode) -> (120, 46.19) -> wire to (110, 46.19) = R1 pin 1
#     Pin 2 (A, anode)   -> (120, 53.81) -> wire to (120, 60)    -> label "VBUS"
#
#   R1 (Device:R) at (110, 50) rotation 0
#     Pin 1 at (110, 46.19) -- shared junction with D1 cathode
#     Pin 2 at (110, 53.81) -> wire to (110, 60) -> label "GND"
#
# Without barriers, BFS from D1 cathode (120, 46.19) traverses:
#   -> (110, 46.19) [R1 pin 1] -> (110, 53.81) [R1 pin 2] -> (110, 60) [GND]
# and incorrectly returns "GND".
#
# With barriers, BFS stops at R1 pin 1 (110, 46.19) because it belongs to
# another component.  D1 cathode should resolve to None (unnamed net).
# ---------------------------------------------------------------------------
DIODE_RESISTOR_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000020")
  (paper "A4")
  (lib_symbols
    (symbol "Device:D_Schottky"
      (symbol "D_Schottky_0_1"
      )
      (symbol "D_Schottky_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "K")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "A")
          (number "2")
        )
      )
    )
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270)
          (length 1.27)
          (name "~")
          (number "1")
        )
        (pin passive line
          (at 0 -3.81 90)
          (length 1.27)
          (name "~")
          (number "2")
        )
      )
    )
  )
  (symbol
    (lib_id "Device:D_Schottky")
    (at 120 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "d1-uuid")
    (property "Reference" "D1" (at 122 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "BAT54" (at 122 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "d1p1"))
    (pin "2" (uuid "d1p2"))
  )
  (symbol
    (lib_id "Device:R")
    (at 110 50 0)
    (unit 1)
    (in_bom yes)
    (on_board yes)
    (dnp no)
    (uuid "r1-uuid")
    (property "Reference" "R1" (at 112 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "10k" (at 112 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "r1p1"))
    (pin "2" (uuid "r1p2"))
  )
  (wire (pts (xy 120 46.19) (xy 110 46.19))
    (stroke (width 0) (type default)) (uuid "w-d1k-r1p1"))
  (wire (pts (xy 120 53.81) (xy 120 60))
    (stroke (width 0) (type default)) (uuid "w-d1a-vbus"))
  (wire (pts (xy 110 53.81) (xy 110 60))
    (stroke (width 0) (type default)) (uuid "w-r1p2-gnd"))
  (label "VBUS" (at 120 60 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-vbus"))
  (label "GND" (at 110 60 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-gnd"))
)
"""


# ---------------------------------------------------------------------------
# Schematic modelling the false-positive from issue #2116.
#
# A pull-up resistor (R1) has pin 2 connected via a short vertical wire to a
# horizontal wire that also passes through an IC pin (U1 pin 1).  The net
# label "SCL" is at the far end of the horizontal wire -- on the opposite
# side of U1's pin.
#
# Topology:
#   Global label "SCL" at (80, 50)
#   Horizontal wire (80, 50) -> (110, 50)
#   U1 (generic IC) at (110, 54.81):
#     Pin 1 at (110, 51.0)  -- sits on the horizontal wire (split point)
#   R1 (Device:R) at (110, 40) rotation 0:
#     Pin 1 at (110, 36.19) -> wire up to power symbol +3.3V at (110, 30)
#     Pin 2 at (110, 43.81) -> wire down to (110, 50) on horizontal wire
#
# Before fix: BFS from R1 pin 2 reaches (110, 50), then hits U1 pin 1 at
# (110, 51.0).  But wait, (110, 50) is NOT U1's pin -- the pin is at
# (110, 51.0).  Actually the wire endpoint (110, 50) and U1 pin (110, 51.0)
# are different points.  Let me recalculate to make the bug reproduce.
#
# Revised topology (matches issue #2116 pattern exactly):
#   Global label "SCL" at (80, 50)
#   Horizontal wire (80, 50) -> (110, 50)
#   U1 pin 1 is at exactly (110, 50) -- the wire endpoint.
#   R1 pin 2 at (110, 46.19) connects via vertical wire to (110, 50).
#
# Before fix: BFS from R1 pin 2 (110, 46.19) goes to (110, 50), which is
# a barrier pin (U1 pin 1).  BFS checks net_names at (110, 50) -- no label
# there (label is at (80, 50)).  BFS stops.  R1 pin 2 -> net=None.
#
# After fix: _propagate_net_names pre-fills (110, 50) with "SCL" because
# it's wire-connected to the label at (80, 50).  BFS finds "SCL" at the
# barrier pin and returns it.
# ---------------------------------------------------------------------------
PULLUP_BARRIER_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000002116")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line
          (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
    (symbol "MyLib:IC"
      (symbol "IC_1_1"
        (pin input line
          (at -3.81 0 0) (length 1.27) (name "SCL") (number "1"))
        (pin input line
          (at -3.81 -2.54 0) (length 1.27) (name "SDA") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 110 40 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "r1-pullup")
    (property "Reference" "R1" (at 112 38 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "4.7k" (at 112 40 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "r1p1")) (pin "2" (uuid "r1p2"))
  )
  (symbol
    (lib_id "MyLib:IC") (at 113.81 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "u1-ic")
    (property "Reference" "U1" (at 116 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "PCM5122" (at 116 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "u1p1")) (pin "2" (uuid "u1p2"))
  )
  (wire (pts (xy 80 50) (xy 110 50))
    (stroke (width 0) (type default)) (uuid "w-scl-bus"))
  (wire (pts (xy 110 43.81) (xy 110 50))
    (stroke (width 0) (type default)) (uuid "w-r1p2-bus"))
  (wire (pts (xy 110 36.19) (xy 110 30))
    (stroke (width 0) (type default)) (uuid "w-r1p1-vcc"))
  (symbol
    (lib_id "power:+3.3V")
    (at 110 30 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no)
    (uuid "pwr-33v")
    (property "Reference" "#PWR01" (at 110 26 0)
      (effects (font (size 1.27 1.27)) hide))
    (property "Value" "+3.3V" (at 110 26 0)
      (effects (font (size 1.27 1.27))))
    (pin "1" (uuid "pwr1"))
  )
  (global_label "SCL" (at 80 50 180)
    (effects (font (size 1.27 1.27)) (justify right))
    (uuid "gl-scl"))
)
"""


def _write_sch(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.kicad_sch"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Unit tests: coordinate conversion
# ---------------------------------------------------------------------------


class TestToCoord:
    def test_basic(self):
        assert _to_coord(100.0, 50.0) == (1000, 500)

    def test_fractional(self):
        assert _to_coord(46.19, 3.81) == (462, 38)

    def test_rounding(self):
        # 0.05 * 10 = 0.5, rounds to 0 (banker's rounding) or 1
        coord = _to_coord(10.05, 20.15)
        assert coord == (100, 202) or coord == (101, 202)


# ---------------------------------------------------------------------------
# Unit tests: wire graph building
# ---------------------------------------------------------------------------


class TestBuildWireGraph:
    def test_basic_graph(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # Should have wire endpoint nodes
        assert len(adjacency) > 0

        # Labels should appear in net_names
        vin_coord = _to_coord(100, 40)
        gnd_coord = _to_coord(100, 60)
        assert net_names[vin_coord] == "VIN"
        assert net_names[gnd_coord] == "GND"

    def test_power_symbol_in_net_names(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, POWER_SYMBOL_SCHEMATIC))
        _, net_names = _build_wire_graph(sch)

        power_coord = _to_coord(100, 40)
        assert net_names[power_coord] == "+3.3V"


# ---------------------------------------------------------------------------
# Unit tests: wire splitting for labels on midpoints
# ---------------------------------------------------------------------------


class TestPointOnSegment:
    def test_midpoint(self):
        assert _point_on_segment((500, 500), (0, 500), (1000, 500)) is True

    def test_endpoint_excluded(self):
        assert _point_on_segment((0, 500), (0, 500), (1000, 500)) is False

    def test_off_segment(self):
        assert _point_on_segment((500, 600), (0, 500), (1000, 500)) is False

    def test_vertical_wire(self):
        assert _point_on_segment((100, 500), (100, 0), (100, 1000)) is True


class TestLabelOnWireMidpoint:
    """Labels placed on the middle of a wire (not at endpoints) must be reachable."""

    def test_label_midpoint_resolution(self, tmp_path):
        """A label at (110, 40) on a wire from (100, 40) to (120, 40)."""
        sch_content = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000010")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line
          (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "ff01")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w1"))
  (wire (pts (xy 100 40) (xy 120 40))
    (stroke (width 0) (type default)) (uuid "w2"))
  (label "SIG" (at 110 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-sig"))
)
"""
        sch = Schematic.load(_write_sch(tmp_path, sch_content))
        pin_map = resolve_pin_map(sch)

        # R1 pin 1 at (100, 46.19) -> wire to (100,40) -> wire to (110,40) label "SIG"
        assert pin_map["R1"]["pins"]["1"]["net"] == "SIG"


# ---------------------------------------------------------------------------
# Unit tests: flood fill
# ---------------------------------------------------------------------------


class TestFloodFill:
    def test_direct_label(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # R1 pin 1 at (100, 46.19) -> wire to (100, 40) -> label "VIN"
        pin_coord = _to_coord(100, 46.19)
        net = _flood_fill_net(pin_coord, adjacency, net_names)
        assert net == "VIN"

    def test_chain_through_wire(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # C1 pin 1 at (120, 46.19) -> wire to (120, 40) -> wire to (100, 40) -> "VIN"
        pin_coord = _to_coord(120, 46.19)
        net = _flood_fill_net(pin_coord, adjacency, net_names)
        assert net == "VIN"

    def test_no_label(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, UNCONNECTED_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # R1 pin 1 at (100, 46.19) (after Y-negation), no wires at all
        pin_coord = _to_coord(100, 46.19)
        net = _flood_fill_net(pin_coord, adjacency, net_names)
        assert net is None


# ---------------------------------------------------------------------------
# Unit tests: resolve_pin_map
# ---------------------------------------------------------------------------


class TestResolvePinMap:
    def test_basic_resolution(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        assert "C1" in pin_map

        # Pin 1 lib (0, 3.81) -> negate Y -> (0, -3.81) -> schematic (100, 46.19) -> VIN
        # Pin 2 lib (0, -3.81) -> negate Y -> (0, 3.81) -> schematic (100, 53.81) -> GND
        assert pin_map["R1"]["pins"]["1"]["net"] == "VIN"
        assert pin_map["R1"]["pins"]["2"]["net"] == "GND"
        assert pin_map["R1"]["lib_id"] == "Device:R"

        # Verify absolute pin positions (R1 at (100, 50), pin offsets +-3.81)
        assert pin_map["R1"]["pins"]["1"]["position"] == [100.0, 46.19]
        assert pin_map["R1"]["pins"]["2"]["position"] == [100.0, 53.81]

        # C1 follows the same pin layout (at (120, 50))
        assert pin_map["C1"]["pins"]["1"]["net"] == "VIN"
        assert pin_map["C1"]["pins"]["2"]["net"] == "GND"
        assert pin_map["C1"]["pins"]["1"]["position"] == [120.0, 46.19]
        assert pin_map["C1"]["pins"]["2"]["position"] == [120.0, 53.81]

    def test_pin_type(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert pin_map["R1"]["pins"]["1"]["type"] == "passive"

    def test_ref_filter(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        pin_map = resolve_pin_map(sch, ref_filter="R1")

        assert "R1" in pin_map
        assert "C1" not in pin_map

    def test_ref_filter_no_match(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        pin_map = resolve_pin_map(sch, ref_filter="U99")

        assert len(pin_map) == 0

    def test_power_symbol_net(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, POWER_SYMBOL_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        # R1 pin 1 at (100, 46.19) connected via wire to +3.3V power symbol at (100, 40)
        assert pin_map["R1"]["pins"]["1"]["net"] == "+3.3V"
        # R1 pin 2 at (100, 53.81) is unconnected (no wire)
        assert pin_map["R1"]["pins"]["2"]["net"] is None

    def test_power_symbols_excluded(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, POWER_SYMBOL_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        # Power symbols should not appear as components
        for ref in pin_map:
            assert not ref.startswith("#PWR")

    def test_unconnected_pin(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, UNCONNECTED_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        assert pin_map["R1"]["pins"]["1"]["net"] is None
        assert pin_map["R1"]["pins"]["2"]["net"] is None
        # Position should still be present even for unconnected pins
        assert pin_map["R1"]["pins"]["1"]["position"] == [100.0, 46.19]
        assert pin_map["R1"]["pins"]["2"]["position"] == [100.0, 53.81]

    def test_rotated_symbol_positions(self, tmp_path):
        """Symbol rotated 90 degrees: pin offsets rotate accordingly.

        KiCad library coordinates use Y-up; schematic coordinates use Y-down.
        After negating Y, pin 1 (0, 3.81) becomes (0, -3.81), which rotated
        90 degrees yields (3.81, 0), giving absolute (103.81, 50).
        """
        sch = Schematic.load(_write_sch(tmp_path, ROTATED_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        pos1 = pin_map["R1"]["pins"]["1"]["position"]
        pos2 = pin_map["R1"]["pins"]["2"]["position"]
        # Pin 1: (0, 3.81) negate Y -> (0, -3.81) rotated 90 -> (3.81, 0) + (100, 50)
        assert abs(pos1[0] - 103.81) < 0.01
        assert abs(pos1[1] - 50.0) < 0.01
        # Pin 2: (0, -3.81) negate Y -> (0, 3.81) rotated 90 -> (-3.81, 0) + (100, 50)
        assert abs(pos2[0] - 96.19) < 0.01
        assert abs(pos2[1] - 50.0) < 0.01

    def test_mirrored_symbol_positions(self, tmp_path):
        """Symbol mirrored in X: for a symmetric resistor with pins on the Y-axis,
        mirror X (which negates X) does not change pin positions.

        After Y-negation, pin 1 is at (0, -3.81) and pin 2 at (0, 3.81).
        Mirror X negates X which is 0, so positions are unchanged.
        """
        sch = Schematic.load(_write_sch(tmp_path, MIRRORED_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        pos1 = pin_map["R1"]["pins"]["1"]["position"]
        pos2 = pin_map["R1"]["pins"]["2"]["position"]
        # Mirror X on symmetric resistor: positions same as non-mirrored (with Y fix)
        assert abs(pos1[0] - 100.0) < 0.01
        assert abs(pos1[1] - 46.19) < 0.01
        assert abs(pos2[0] - 100.0) < 0.01
        assert abs(pos2[1] - 53.81) < 0.01


# ---------------------------------------------------------------------------
# Unit tests: BFS barrier prevents traversal through other components
# ---------------------------------------------------------------------------


class TestBFSBarrier:
    """Verify that net tracing does not traverse through another component's body."""

    def test_diode_cathode_does_not_resolve_through_resistor(self, tmp_path):
        """D1 cathode connects to R1 pin 1 via wire.  R1 pin 2 connects to GND.

        Without BFS barriers, D1 cathode would incorrectly resolve to GND by
        traversing through R1's body.  With barriers, the BFS stops at R1 pin 1
        and D1 cathode resolves to None (unnamed net at the junction).
        """
        sch = Schematic.load(_write_sch(tmp_path, DIODE_RESISTOR_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "D1" in pin_map
        assert "R1" in pin_map

        # D1 cathode (pin 1) must NOT resolve to GND
        d1_pin1_net = pin_map["D1"]["pins"]["1"]["net"]
        assert d1_pin1_net is None, (
            f"D1 cathode should be None (unnamed net), got {d1_pin1_net!r}"
        )

        # D1 anode (pin 2) should resolve to VBUS
        assert pin_map["D1"]["pins"]["2"]["net"] == "VBUS"

        # R1 should still resolve correctly
        # R1 pin 1 shares junction with D1 cathode -- unnamed net
        assert pin_map["R1"]["pins"]["1"]["net"] is None
        # R1 pin 2 connects to GND
        assert pin_map["R1"]["pins"]["2"]["net"] == "GND"

    def test_shared_junction_still_resolves(self, tmp_path):
        """Components sharing a junction (same coordinate) should both see the
        net name at that junction, even with barriers enabled."""
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        # R1 and C1 share the VIN junction at (100, 40) and GND at (100, 60)
        assert pin_map["R1"]["pins"]["1"]["net"] == "VIN"
        assert pin_map["C1"]["pins"]["1"]["net"] == "VIN"
        assert pin_map["R1"]["pins"]["2"]["net"] == "GND"
        assert pin_map["C1"]["pins"]["2"]["net"] == "GND"


# ---------------------------------------------------------------------------
# Regression: pull-up resistor wire meets IC pin (barrier) before label
# ---------------------------------------------------------------------------


class TestPullupBarrierNetPropagation:
    """Regression test for issue #2116: pull-up resolves to net=None when its
    wire connects at the same coordinate as another component's pin (barrier),
    and the net label is on the far side of that shared node."""

    def test_pullup_resolves_through_barrier_pin(self, tmp_path):
        """R1 pin 2 connects via wire to (110,50) which is also U1 pin 1.
        The SCL label is at (80,50) on the other side of U1's pin.

        Before the fix, R1 pin 2 resolved to None because BFS stopped at
        the barrier (U1 pin 1) and no net name was recorded there.
        After the fix, net name propagation ensures (110,50) has 'SCL'."""
        sch = Schematic.load(_write_sch(tmp_path, PULLUP_BARRIER_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        assert "U1" in pin_map

        # R1 pin 2 must resolve to SCL (was None before fix)
        r1_pin2_net = pin_map["R1"]["pins"]["2"]["net"]
        assert r1_pin2_net == "SCL", (
            f"R1 pin 2 should resolve to 'SCL', got {r1_pin2_net!r}"
        )

        # R1 pin 1 should resolve to +3.3V (power symbol)
        assert pin_map["R1"]["pins"]["1"]["net"] == "+3.3V"

        # U1 pin 1 should also resolve to SCL
        assert pin_map["U1"]["pins"]["1"]["net"] == "SCL"

    def test_barrier_still_prevents_cross_component_traversal(self, tmp_path):
        """Even with net propagation, BFS must not cross through a component.

        The diode-resistor test (DIODE_RESISTOR_SCHEMATIC) must still pass:
        D1 cathode must NOT resolve to GND through R1's body."""
        sch = Schematic.load(_write_sch(tmp_path, DIODE_RESISTOR_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        d1_pin1_net = pin_map["D1"]["pins"]["1"]["net"]
        assert d1_pin1_net is None, (
            f"D1 cathode should still be None, got {d1_pin1_net!r}"
        )
        assert pin_map["D1"]["pins"]["2"]["net"] == "VBUS"
        assert pin_map["R1"]["pins"]["2"]["net"] == "GND"


# ---------------------------------------------------------------------------
# Pin-to-pin connectivity through passive filter networks
# ---------------------------------------------------------------------------


# RC filter chain: R1 and C1 connected via a shared horizontal bus wire.
# Label "AUDIO_L" at one end of a long wire. R1 pin 1 and C1 pin 1 both
# connect to points on that wire (their pin coordinates land on the wire interior).
#
# Layout:
#   AUDIO_L label at (80, 40)
#   Wire from (80, 40) to (140, 40)  -- long horizontal bus
#   R1 at (100, 50): pin 1 at (100, 46.19), wire down to (100, 40) on bus
#   C1 at (120, 50): pin 1 at (120, 46.19), wire down to (120, 40) on bus
#   R1 pin 2 at (100, 53.81), C1 pin 2 at (120, 53.81) -- both floating
RC_FILTER_CHAIN_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000020")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line
          (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
    (symbol "Device:C"
      (symbol "C_1_1"
        (pin passive line
          (at 0 3.81 270) (length 2.794) (name "~") (number "1"))
        (pin passive line
          (at 0 -3.81 90) (length 2.794) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "rc01")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "100" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (symbol
    (lib_id "Device:C") (at 120 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "rc02")
    (property "Reference" "C1" (at 122 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "100nF" (at 122 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p3")) (pin "2" (uuid "p4"))
  )
  (wire (pts (xy 80 40) (xy 140 40))
    (stroke (width 0) (type default)) (uuid "w-bus"))
  (wire (pts (xy 100 46.19) (xy 100 40))
    (stroke (width 0) (type default)) (uuid "w-r1"))
  (wire (pts (xy 120 46.19) (xy 120 40))
    (stroke (width 0) (type default)) (uuid "w-c1"))
  (label "AUDIO_L" (at 80 40 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-audio"))
)
"""

# Pin on wire interior: R1 pin 1 at (100, 46.19) sits on a long wire from (100, 30) to (100, 60).
# Label "NET1" at (100, 30). The pin coordinate is in the interior of the wire, not at an endpoint.
PIN_ON_WIRE_INTERIOR_SCHEMATIC = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000030")
  (paper "A4")
  (lib_symbols
    (symbol "Device:R"
      (symbol "R_1_1"
        (pin passive line
          (at 0 3.81 270) (length 1.27) (name "~") (number "1"))
        (pin passive line
          (at 0 -3.81 90) (length 1.27) (name "~") (number "2"))
      )
    )
  )
  (symbol
    (lib_id "Device:R") (at 100 50 0) (unit 1)
    (in_bom yes) (on_board yes) (dnp no) (uuid "int01")
    (property "Reference" "R1" (at 102 48 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (property "Value" "1k" (at 102 50 0)
      (effects (font (size 1.27 1.27)) (justify left)))
    (pin "1" (uuid "p1")) (pin "2" (uuid "p2"))
  )
  (wire (pts (xy 100 30) (xy 100 60))
    (stroke (width 0) (type default)) (uuid "w1"))
  (label "NET1" (at 100 30 0)
    (effects (font (size 1.27 1.27)) (justify left bottom))
    (uuid "lbl-net1"))
)
"""


class TestRCFilterChain:
    """Pins connected through a passive filter chain must all resolve to the net label."""

    def test_all_chain_pins_resolve(self, tmp_path):
        """R1 and C1 pin 1 both connect to a horizontal bus wire with label AUDIO_L."""
        sch = Schematic.load(_write_sch(tmp_path, RC_FILTER_CHAIN_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert pin_map["R1"]["pins"]["1"]["net"] == "AUDIO_L"
        assert pin_map["C1"]["pins"]["1"]["net"] == "AUDIO_L"
        # Pin 2 of both components has no wire -- should be None
        assert pin_map["R1"]["pins"]["2"]["net"] is None
        assert pin_map["C1"]["pins"]["2"]["net"] is None

    def test_ref_filter_with_chain(self, tmp_path):
        """Filtering by ref still resolves nets correctly in a chain."""
        sch = Schematic.load(_write_sch(tmp_path, RC_FILTER_CHAIN_SCHEMATIC))
        pin_map = resolve_pin_map(sch, ref_filter="C1")

        assert "C1" in pin_map
        assert "R1" not in pin_map
        assert pin_map["C1"]["pins"]["1"]["net"] == "AUDIO_L"


class TestPinOnWireInterior:
    """A pin whose coordinate lands on the interior of a wire segment (not at an endpoint)."""

    def test_pin_interior_resolves(self, tmp_path):
        """R1 pin 1 at (100, 46.19) is in the interior of wire (100,30)-(100,60)."""
        sch = Schematic.load(_write_sch(tmp_path, PIN_ON_WIRE_INTERIOR_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert pin_map["R1"]["pins"]["1"]["net"] == "NET1"

    def test_pin_interior_both_pins(self, tmp_path):
        """Both R1 pins land on the interior of the same long wire."""
        sch = Schematic.load(_write_sch(tmp_path, PIN_ON_WIRE_INTERIOR_SCHEMATIC))
        pin_map = resolve_pin_map(sch)

        assert pin_map["R1"]["pins"]["1"]["net"] == "NET1"
        assert pin_map["R1"]["pins"]["2"]["net"] == "NET1"


# ---------------------------------------------------------------------------
# Integration tests: real fixture
# ---------------------------------------------------------------------------


class TestWithFixture:
    @pytest.fixture
    def simple_rc_path(self):
        return Path(__file__).parent / "fixtures" / "simple_rc.kicad_sch"

    def test_fixture_loads(self, simple_rc_path):
        if not simple_rc_path.exists():
            pytest.skip("Fixture not available")

        sch = Schematic.load(simple_rc_path)
        pin_map = resolve_pin_map(sch)

        assert "R1" in pin_map
        assert "C1" in pin_map

        # Both should have 2 pins each
        assert len(pin_map["R1"]["pins"]) == 2
        assert len(pin_map["C1"]["pins"]) == 2


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestCLI:
    def test_json_output(self, tmp_path, capsys):
        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        rc = pin_map_main([str(path), "--format", "json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "R1" in data
        assert "C1" in data
        assert data["R1"]["pins"]["1"]["net"] == "VIN"
        # Verify position is present in JSON output
        assert data["R1"]["pins"]["1"]["position"] == [100.0, 46.19]
        assert data["R1"]["pins"]["2"]["position"] == [100.0, 53.81]

    def test_table_output(self, tmp_path, capsys):
        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        rc = pin_map_main([str(path), "--format", "table"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "R1" in captured.out
        assert "VIN" in captured.out
        assert "GND" in captured.out
        # Verify Position column header and coordinate values appear
        assert "Position" in captured.out
        assert "100.00" in captured.out

    def test_ref_filter_cli(self, tmp_path, capsys):
        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        rc = pin_map_main([str(path), "--ref", "C1", "--format", "json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "C1" in data
        assert "R1" not in data

    def test_missing_file(self, tmp_path, capsys):
        rc = pin_map_main([str(tmp_path / "nonexistent.kicad_sch")])
        assert rc == 1

    def test_default_format_is_json(self, tmp_path, capsys):
        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        rc = pin_map_main([str(path)])

        assert rc == 0
        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.out)
        assert isinstance(data, dict)

    def test_empty_schematic(self, tmp_path, capsys):
        """Schematic with no symbols should produce empty JSON output."""
        empty_sch = """\
(kicad_sch
  (version 20231120)
  (generator "test")
  (uuid "00000000-0000-0000-0000-000000000099")
  (paper "A4")
  (lib_symbols)
)
"""
        path = _write_sch(tmp_path, empty_sch)
        rc = pin_map_main([str(path), "--format", "json"])

        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data == {}


# ---------------------------------------------------------------------------
# Hierarchy traversal tests
# ---------------------------------------------------------------------------

HIERARCHICAL_ROOT = Path(__file__).parent / "fixtures" / "hierarchical" / "root.kicad_sch"


class TestHierarchyTraversal:
    """Verify that pin-map iterates all sheets in a hierarchical design."""

    @pytest.fixture(autouse=True)
    def _skip_if_missing(self):
        if not HIERARCHICAL_ROOT.exists():
            pytest.skip("Hierarchical fixture not available")

    def test_all_sheets_included(self, capsys):
        """Components from root AND child sheets must appear in the output."""
        rc = pin_map_main([str(HIERARCHICAL_ROOT), "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        # root.kicad_sch has R1
        assert "R1" in data, "R1 from root sheet should be present"
        # sub_a.kicad_sch has R2, C1
        assert "R2" in data, "R2 from sub_a sheet should be present"
        assert "C1" in data, "C1 from sub_a sheet should be present"
        # sub_b.kicad_sch has R3, R4
        assert "R3" in data, "R3 from sub_b sheet should be present"
        assert "R4" in data, "R4 from sub_b sheet should be present"
        # nested.kicad_sch has C2
        assert "C2" in data, "C2 from nested sheet should be present"

    def test_ref_filter_finds_child_component(self, capsys):
        """--ref for a component on a child sheet must return it."""
        rc = pin_map_main([str(HIERARCHICAL_ROOT), "--ref", "R3", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        assert "R3" in data, "R3 from sub_b should be found with --ref filter"
        assert len(data) == 1, "Only R3 should be returned"

    def test_ref_filter_no_match(self, capsys):
        """--ref for a non-existent component returns empty across all sheets."""
        rc = pin_map_main([str(HIERARCHICAL_ROOT), "--ref", "U99", "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data == {}

    def test_sheet_filter(self, capsys):
        """--sheet restricts output to components on the matching sheet."""
        rc = pin_map_main([
            str(HIERARCHICAL_ROOT), "--sheet", "sub_b", "--format", "json"
        ])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        # sub_b.kicad_sch has R3 and R4
        assert "R3" in data
        assert "R4" in data
        # Components from other sheets should NOT be present
        assert "R1" not in data
        assert "R2" not in data
        assert "C1" not in data
        assert "C2" not in data

    def test_single_sheet_still_works(self, capsys):
        """A schematic with no child sheets should still work (just root)."""
        # sub_b.kicad_sch has no child sheets
        sub_b = Path(__file__).parent / "fixtures" / "hierarchical" / "sub_b.kicad_sch"
        rc = pin_map_main([str(sub_b), "--format", "json"])
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "R3" in data
        assert "R4" in data
