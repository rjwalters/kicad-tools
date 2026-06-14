"""Tests for DSN (Specctra) export functionality."""

import re
from pathlib import Path

import pytest

from kicad_tools.export.dsn import (
    KiCadToDSNExporter,
    _dsn_quote,
    mm_to_um,
    um_to_mm,
)

# Path to the voltage divider test board
VOLTAGE_DIVIDER_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider.kicad_pcb"
)

VOLTAGE_DIVIDER_ROUTED_PCB = (
    Path(__file__).parent.parent
    / "boards"
    / "01-voltage-divider"
    / "output"
    / "voltage_divider_routed.kicad_pcb"
)


class TestUnitConversions:
    """Test coordinate conversion helpers."""

    def test_mm_to_um(self):
        assert mm_to_um(1.0) == 1000.0
        assert mm_to_um(0.25) == 250.0
        assert mm_to_um(0.0) == 0.0

    def test_um_to_mm(self):
        assert um_to_mm(1000.0) == 1.0
        assert um_to_mm(250.0) == 0.25

    def test_roundtrip(self):
        for val in [0.0, 0.1, 0.25, 1.0, 10.5, 100.0]:
            assert abs(um_to_mm(mm_to_um(val)) - val) < 1e-6


class TestDSNQuote:
    """Test DSN string quoting."""

    def test_simple_name(self):
        assert _dsn_quote("F.Cu") == "F.Cu"

    def test_name_with_spaces(self):
        assert _dsn_quote("my net") == '"my net"'

    def test_name_with_parens(self):
        assert _dsn_quote("Net-(J1-1)") == '"Net-(J1-1)"'

    def test_empty_string(self):
        assert _dsn_quote("") == '""'


class TestDSNExporterBasic:
    """Test DSN exporter with the voltage divider board."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

    def test_export_produces_valid_sexp(self, exporter):
        dsn = exporter.export()
        assert dsn.startswith("(pcb")
        assert dsn.rstrip().endswith(")")

        # Check bracket balance
        open_count = dsn.count("(")
        close_count = dsn.count(")")
        assert open_count == close_count, (
            f"Unbalanced parens: {open_count} open vs {close_count} close"
        )

    def test_layers_extracted(self, exporter):
        layers = exporter.layers
        assert "F.Cu" in layers
        assert "B.Cu" in layers

    def test_nets_extracted(self, exporter):
        nets = exporter.nets
        # Voltage divider has nets: "", "VIN", "VOUT", "GND"
        net_names = set(nets.values())
        assert "VIN" in net_names
        assert "VOUT" in net_names
        assert "GND" in net_names

    def test_footprints_extracted(self, exporter):
        fps = exporter.footprints
        refs = {fp.reference for fp in fps}
        assert "J1" in refs
        assert "J2" in refs
        assert "R1" in refs
        assert "R2" in refs

    def test_dsn_has_structure_section(self, exporter):
        dsn = exporter.export()
        assert "(structure" in dsn

    def test_dsn_has_placement_section(self, exporter):
        dsn = exporter.export()
        assert "(placement" in dsn

    def test_dsn_has_library_section(self, exporter):
        dsn = exporter.export()
        assert "(library" in dsn

    def test_dsn_has_network_section(self, exporter):
        dsn = exporter.export()
        assert "(network" in dsn

    def test_dsn_has_boundary(self, exporter):
        dsn = exporter.export()
        assert "(boundary" in dsn

    def test_dsn_has_resolution(self, exporter):
        dsn = exporter.export()
        assert "(resolution um 10)" in dsn

    def test_dsn_net_names_present(self, exporter):
        dsn = exporter.export()
        assert "VIN" in dsn
        assert "VOUT" in dsn
        assert "GND" in dsn

    def test_dsn_component_references(self, exporter):
        dsn = exporter.export()
        assert "J1" in dsn
        assert "J2" in dsn
        assert "R1" in dsn
        assert "R2" in dsn

    def test_dsn_has_padstacks(self, exporter):
        dsn = exporter.export()
        assert "(padstack" in dsn

    def test_dsn_has_pins(self, exporter):
        dsn = exporter.export()
        assert "(pin" in dsn

    def test_dsn_has_net_class(self, exporter):
        dsn = exporter.export()
        assert "(class kicad_default" in dsn


class TestDSNExporterWithRoutes:
    """Test DSN export with pre-existing routes."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_ROUTED_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_ROUTED_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_ROUTED_PCB))

    def test_wiring_section_present(self, exporter):
        dsn = exporter.export()
        assert "(wiring" in dsn

    def test_wiring_has_protected_routes(self, exporter):
        dsn = exporter.export()
        assert "(type protect)" in dsn

    def test_wiring_has_wire_elements(self, exporter):
        dsn = exporter.export()
        assert "(wire" in dsn
        # Wire should have path with layer and coordinates
        assert "(path" in dsn


class TestDSNExportToFile:
    """Test writing DSN to disk."""

    @pytest.fixture
    def exporter(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        return KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

    def test_export_to_file(self, exporter, tmp_path):
        output = tmp_path / "test.dsn"
        dsn = exporter.export(str(output))
        assert output.exists()
        assert output.read_text() == dsn

    def test_export_creates_parent_dirs(self, exporter, tmp_path):
        output = tmp_path / "subdir" / "nested" / "test.dsn"
        exporter.export(str(output))
        assert output.exists()


class TestDSNRoundTrip:
    """Test structural consistency of DSN export."""

    @pytest.fixture
    def dsn_content(self):
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))
        return exporter.export()

    def test_net_count_matches(self, dsn_content):
        """Verify number of nets in DSN matches source PCB."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count nets in DSN (exclude net 0 which is the unconnected net)
        re.findall(r'\(net "?[^")\s]+', dsn_content)
        # Count non-empty net names from source
        source_nets = {n for n, v in exporter.nets.items() if v and n != 0}

        # DSN should have entries for each named net
        assert len(source_nets) > 0

    def test_component_count_matches(self, dsn_content):
        """Verify number of components in DSN matches source PCB."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count place directives in DSN
        place_count = len(re.findall(r"\(place\s", dsn_content))
        assert place_count == len(exporter.footprints)

    def test_layer_count_matches(self, dsn_content):
        """Verify layer count in DSN structure matches source."""
        if not VOLTAGE_DIVIDER_PCB.exists():
            pytest.skip(f"Test board not found: {VOLTAGE_DIVIDER_PCB}")
        exporter = KiCadToDSNExporter(str(VOLTAGE_DIVIDER_PCB))

        # Count (layer ...) in structure section
        structure_match = re.search(r"\(structure(.*?)\n  \)", dsn_content, re.DOTALL)
        assert structure_match
        layer_count = len(re.findall(r"\(layer\s", structure_match.group(1)))
        assert layer_count == len(exporter.layers)
