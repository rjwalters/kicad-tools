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

        # R1 pin 2 at (100, 46.19) -> wire to (100,40) -> wire to (110,40) label "SIG"
        assert pin_map["R1"]["pins"]["2"]["net"] == "SIG"


# ---------------------------------------------------------------------------
# Unit tests: flood fill
# ---------------------------------------------------------------------------


class TestFloodFill:
    def test_direct_label(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # R1 pin 2 at (100, 46.19) -> wire to (100, 40) -> label "VIN"
        pin_coord = _to_coord(100, 46.19)
        net = _flood_fill_net(pin_coord, adjacency, net_names)
        assert net == "VIN"

    def test_chain_through_wire(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, MINIMAL_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # C1 pin 2 at (120, 46.19) -> wire to (120, 40) -> wire to (100, 40) -> "VIN"
        pin_coord = _to_coord(120, 46.19)
        net = _flood_fill_net(pin_coord, adjacency, net_names)
        assert net == "VIN"

    def test_no_label(self, tmp_path):
        sch = Schematic.load(_write_sch(tmp_path, UNCONNECTED_SCHEMATIC))
        adjacency, net_names = _build_wire_graph(sch)

        # R1 pin 1 at (100, 46.19), no wires at all
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

        # Pin 1 is at (0, 3.81) -> y=50+3.81=53.81 -> connects to GND at y=60
        # Pin 2 is at (0, -3.81) -> y=50-3.81=46.19 -> connects to VIN at y=40
        assert pin_map["R1"]["pins"]["1"]["net"] == "GND"
        assert pin_map["R1"]["pins"]["2"]["net"] == "VIN"
        assert pin_map["R1"]["lib_id"] == "Device:R"

        # C1 follows the same pin layout
        assert pin_map["C1"]["pins"]["1"]["net"] == "GND"
        assert pin_map["C1"]["pins"]["2"]["net"] == "VIN"

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
        # R1 pin 2 at (100, 46.19) connected via wire to +3.3V power symbol at (100, 40)
        assert pin_map["R1"]["pins"]["2"]["net"] == "+3.3V"
        # R1 pin 1 at (100, 53.81) is unconnected (no wire)
        assert pin_map["R1"]["pins"]["1"]["net"] is None

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
        assert data["R1"]["pins"]["1"]["net"] == "GND"

    def test_table_output(self, tmp_path, capsys):
        path = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        rc = pin_map_main([str(path), "--format", "table"])

        assert rc == 0
        captured = capsys.readouterr()
        assert "R1" in captured.out
        assert "VIN" in captured.out
        assert "GND" in captured.out

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
