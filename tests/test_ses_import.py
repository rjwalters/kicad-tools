"""Tests for SES (Specctra Session) import functionality."""

from pathlib import Path

import pytest

from kicad_tools.export.ses import (
    SESToKiCadImporter,
    _build_net_map,
    _extract_balanced,
    _merge_sexp_into_pcb,
)

# Path to the voltage divider test board
VOLTAGE_DIVIDER_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)


# Minimal SES content for testing
MINIMAL_SES = """\
(session voltage_divider
  (base_design voltage_divider)
  (resolution um 10)
  (routes
    (resolution um 10)
    (parser
      (host_cad "Freerouting")
      (host_version "1.9.0")
    )
    (network_out
    )
    (wire
      (path F.Cu 250.0 105000.0 111230.0 105800.0 110400.0)
      (net VIN)
    )
    (wire
      (path F.Cu 250.0 105800.0 110400.0 114000.0 108000.0)
      (net VIN)
    )
    (wire
      (path F.Cu 250.0 116000.0 108000.0 125000.0 111230.0)
      (net VOUT)
    )
    (via
      "Via[0-1]_Pad800_um" 115000.0 112000.0
      (net GND)
    )
  )
)
"""


class TestExtractBalanced:
    """Test the balanced parenthesis extractor."""

    def test_simple(self):
        text = "(hello world)"
        assert _extract_balanced(text, 0) == "(hello world)"

    def test_nested(self):
        text = "(a (b c) d)"
        assert _extract_balanced(text, 0) == "(a (b c) d)"

    def test_with_string(self):
        text = '(net "my net (special)")'
        assert _extract_balanced(text, 0) == '(net "my net (special)")'

    def test_at_offset(self):
        text = "prefix (inner) suffix"
        assert _extract_balanced(text, 7) == "(inner)"

    def test_not_at_paren(self):
        assert _extract_balanced("hello", 0) is None

    def test_out_of_range(self):
        assert _extract_balanced("(a)", 10) is None

    def test_unbalanced(self):
        assert _extract_balanced("(a (b)", 0) is None


class TestBuildNetMap:
    """Test the net name-to-number mapping builder."""

    def test_basic_net_map(self):
        content = """\
(kicad_pcb
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")
  (net 3 "GND")
)"""
        net_map = _build_net_map(content)
        assert net_map["VIN"] == 1
        assert net_map["VOUT"] == 2
        assert net_map["GND"] == 3
        assert "" not in net_map  # empty net name excluded

    def test_net_with_special_chars(self):
        content = '  (net 5 "Net-(J1-1)")\n'
        net_map = _build_net_map(content)
        assert net_map["Net-(J1-1)"] == 5


class TestMergeSexpIntoPcb:
    """Test the PCB merge helper."""

    def test_basic_merge(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n)\n'
        route = '  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'
        merged = _merge_sexp_into_pcb(pcb, route)
        assert "(segment" in merged
        assert merged.rstrip().endswith(")")

    def test_empty_routes(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n)\n'
        merged = _merge_sexp_into_pcb(pcb, "")
        assert merged == pcb

    def test_preserves_pcb_content(self):
        pcb = '(kicad_pcb\n  (net 0 "")\n  (net 1 "VIN")\n)\n'
        route = '  (segment (start 1 2) (end 3 4) (width 0.25) (layer "F.Cu") (net 1))'
        merged = _merge_sexp_into_pcb(pcb, route)
        assert '(net 1 "VIN")' in merged


class TestSESParser:
    """Test SES file parsing."""

    @pytest.fixture
    def importer(self, tmp_path):
        ses_file = tmp_path / "test.ses"
        ses_file.write_text(MINIMAL_SES)
        imp = SESToKiCadImporter(str(ses_file))
        imp.parse()
        return imp

    def test_wires_parsed(self, importer):
        assert len(importer.wires) == 3

    def test_vias_parsed(self, importer):
        assert len(importer.vias) == 1

    def test_wire_net_name(self, importer):
        net_names = {w.net_name for w in importer.wires}
        assert "VIN" in net_names
        assert "VOUT" in net_names

    def test_wire_layer(self, importer):
        for wire in importer.wires:
            assert wire.layer == "F.Cu"

    def test_wire_points(self, importer):
        vin_wires = [w for w in importer.wires if w.net_name == "VIN"]
        assert len(vin_wires) == 2
        # First VIN wire: two points
        assert len(vin_wires[0].points) == 2

    def test_wire_width(self, importer):
        for wire in importer.wires:
            assert wire.width == 250.0

    def test_via_net_name(self, importer):
        assert importer.vias[0].net_name == "GND"

    def test_via_coordinates(self, importer):
        via = importer.vias[0]
        assert via.x == 115000.0
        assert via.y == 112000.0

    def test_via_padstack(self, importer):
        via = importer.vias[0]
        assert "Via" in via.padstack_name


class TestSESMerge:
    """Test merging SES routes into a PCB file."""

    @pytest.fixture
    def ses_file(self, tmp_path):
        f = tmp_path / "test.ses"
        f.write_text(MINIMAL_SES)
        return f

    def test_merge_into_pcb(self, ses_file, tmp_path):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        assert output.exists()
        content = output.read_text()

        # Original content preserved
        assert "(kicad_pcb" in content
        assert '(net 1 "VIN")' in content

        # New routes added
        assert "(segment" in content
        assert "(via" in content

    def test_merge_coordinate_conversion(self, ses_file, tmp_path):
        """Verify SES um coordinates are correctly converted to mm."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # 105000.0 um = 105.0 mm
        assert "105.0" in result

    def test_merge_net_mapping(self, ses_file, tmp_path):
        """Verify SES net names are mapped to correct KiCad net numbers."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # VIN = net 1, VOUT = net 2, GND = net 3
        assert "(net 1)" in result  # VIN segments
        assert "(net 2)" in result  # VOUT segments
        assert "(net 3)" in result  # GND via

    def test_merge_produces_valid_sexp(self, ses_file, tmp_path):
        """Verify merged PCB has balanced parentheses."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        output = tmp_path / "merged.kicad_pcb"
        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        open_count = result.count("(")
        close_count = result.count(")")
        assert open_count == close_count, f"Unbalanced: {open_count} open vs {close_count} close"


class TestSESEmptyRoutes:
    """Test behavior with SES files containing no routes."""

    def test_empty_ses(self, tmp_path):
        ses_file = tmp_path / "empty.ses"
        ses_file.write_text("(session test\n  (routes\n  )\n)")
        importer = SESToKiCadImporter(str(ses_file))
        importer.parse()
        assert len(importer.wires) == 0
        assert len(importer.vias) == 0

    def test_merge_empty_ses(self, tmp_path):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")

        ses_file = tmp_path / "empty.ses"
        ses_file.write_text("(session test\n  (routes\n  )\n)")
        output = tmp_path / "out.kicad_pcb"

        importer = SESToKiCadImporter(str(ses_file))
        result = importer.merge_into(str(VOLTAGE_DIVIDER_PCB), str(output))

        # Should be identical to original
        original = VOLTAGE_DIVIDER_PCB.read_text()
        assert result == original
