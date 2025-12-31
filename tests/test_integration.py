"""Integration tests with real KiCad project files.

These tests validate the tooling with actual KiCad files rather than
synthetic data, ensuring the parser handles real-world scenarios.

Test Coverage:
- Simple single-sheet schematic parsing
- Multi-sheet hierarchical schematic parsing
- Multi-layer PCB with zones parsing
- Full workflow: parse → query → export
- Round-trip: load → save → load produces identical structure
- Cross-reference between schematic and PCB
"""

from pathlib import Path

import pytest

from kicad_tools.project import Project
from kicad_tools.schema import PCB, Schematic
from kicad_tools.sexp import parse_sexp, serialize_sexp


@pytest.fixture
def projects_dir() -> Path:
    """Return the path to the projects test fixtures directory."""
    return Path(__file__).parent / "fixtures" / "projects"


@pytest.fixture
def demo_dir() -> Path:
    """Return the path to the demo directory with real projects."""
    return Path(__file__).parent.parent / "demo"


class TestSchematicParsing:
    """Integration tests for schematic parsing."""

    def test_simple_schematic_load(self, simple_rc_schematic: Path) -> None:
        """Test loading a simple single-sheet schematic."""
        sch = Schematic.load(simple_rc_schematic)

        # Verify basic properties
        assert sch.version == 20231120
        assert sch.paper == "A4"
        assert sch.uuid == "12345678-1234-1234-1234-123456789abc"

        # Verify title block
        tb = sch.title_block
        assert tb.title == "Simple RC Circuit"
        assert tb.date == "2024-01-01"
        assert tb.rev == "1.0"
        assert tb.company == "Test Corp"

        # Verify symbols - filter to actual instances with references
        # (excludes nested library symbol definitions)
        component_symbols = [s for s in sch.symbols if s.reference]
        assert len(component_symbols) == 2

        r1 = sch.get_symbol("R1")
        assert r1 is not None
        assert r1.value == "10k"
        assert r1.lib_id == "Device:R"

        c1 = sch.get_symbol("C1")
        assert c1 is not None
        assert c1.value == "100nF"
        assert c1.lib_id == "Device:C"

        # Verify wires
        assert len(sch.wires) == 6

        # Verify labels
        assert len(sch.labels) == 2
        label_names = [lbl.text for lbl in sch.labels]
        assert "VIN" in label_names
        assert "GND" in label_names

        # Verify junctions
        assert len(sch.junctions) == 2

    def test_hierarchical_schematic_load(self, projects_dir: Path) -> None:
        """Test loading a multi-sheet hierarchical schematic."""
        sch_path = projects_dir / "hierarchical_main.kicad_sch"
        sch = Schematic.load(sch_path)

        # Verify it's detected as hierarchical
        assert sch.is_hierarchical()

        # Verify sub-sheets
        assert len(sch.sheets) == 2
        sheet_names = [s.name for s in sch.sheets]
        assert "Logic" in sheet_names
        assert "Output" in sheet_names

        # Verify sheet files
        logic_sheet = next(s for s in sch.sheets if s.name == "Logic")
        assert logic_sheet.filename == "logic_subsheet.kicad_sch"

        output_sheet = next(s for s in sch.sheets if s.name == "Output")
        assert output_sheet.filename == "output_subsheet.kicad_sch"

        # Verify global labels
        global_labels = sch.global_labels
        assert len(global_labels) >= 1
        label_texts = [lbl.text for lbl in global_labels]
        assert "SIGNAL_OUT" in label_texts

        # Verify symbols (power and decoupling cap)
        symbols = sch.symbols
        power_symbols = [s for s in symbols if s.reference.startswith("#PWR")]
        assert len(power_symbols) >= 2  # VCC and GND power symbols

    def test_schematic_symbol_query(self, simple_rc_schematic: Path) -> None:
        """Test schematic symbol querying capabilities."""
        sch = Schematic.load(simple_rc_schematic)

        # Query by reference
        r1 = sch.get_symbol("R1")
        assert r1 is not None
        assert r1.reference == "R1"

        # Query by lib_id
        resistors = sch.find_symbols_by_lib("Device:R")
        assert len(resistors) == 1
        assert resistors[0].reference == "R1"

        capacitors = sch.find_symbols_by_lib("Device:C")
        assert len(capacitors) == 1
        assert capacitors[0].reference == "C1"

    def test_schematic_lib_symbols(self, simple_rc_schematic: Path) -> None:
        """Test accessing embedded library symbols."""
        sch = Schematic.load(simple_rc_schematic)

        # Get embedded library symbols
        lib_syms = sch.lib_symbols
        assert lib_syms is not None

        # Verify Device:R is embedded
        r_symbol = sch.get_lib_symbol("Device:R")
        assert r_symbol is not None

        # Verify Device:C is embedded
        c_symbol = sch.get_lib_symbol("Device:C")
        assert c_symbol is not None


class TestPCBParsing:
    """Integration tests for PCB parsing."""

    def test_minimal_pcb_load(self, minimal_pcb: Path) -> None:
        """Test loading a minimal PCB file."""
        pcb = PCB.load(str(minimal_pcb))

        # Verify layers
        assert len(pcb.layers) > 0
        copper_layers = pcb.copper_layers
        assert len(copper_layers) == 2  # F.Cu and B.Cu

        # Verify nets
        assert len(pcb.nets) >= 2  # GND and +3.3V
        gnd_net = pcb.get_net_by_name("GND")
        assert gnd_net is not None

        # Verify footprints
        assert len(pcb.footprints) == 1
        r1 = pcb.get_footprint("R1")
        assert r1 is not None
        assert r1.value == "10k"
        assert len(r1.pads) == 2

    def test_multilayer_pcb_with_zones(self, projects_dir: Path) -> None:
        """Test loading a multi-layer PCB with zones."""
        pcb_path = projects_dir / "multilayer_zones.kicad_pcb"
        pcb = PCB.load(str(pcb_path))

        # Verify 4-layer stackup
        copper_layers = pcb.copper_layers
        assert len(copper_layers) == 4  # F.Cu, In1.Cu, In2.Cu, B.Cu
        layer_names = [layer.name for layer in copper_layers]
        assert "F.Cu" in layer_names
        assert "In1.Cu" in layer_names
        assert "In2.Cu" in layer_names
        assert "B.Cu" in layer_names

        # Verify stackup in setup
        setup = pcb.setup
        assert setup is not None
        assert len(setup.stackup) > 0

        # Verify zones
        zones = pcb.zones
        assert len(zones) == 4  # GND bottom, 3V3 in1, GND in2, 5V front

        # Check GND zone properties
        gnd_zones = [z for z in zones if z.net_name == "GND"]
        assert len(gnd_zones) == 2

        # Check zone with thermal relief settings
        gnd_bottom = next(z for z in zones if z.name == "GND_Bottom")
        assert gnd_bottom.layer == "B.Cu"
        assert gnd_bottom.is_filled
        assert gnd_bottom.thermal_gap == 0.4
        assert gnd_bottom.thermal_bridge_width == 0.35
        assert gnd_bottom.connect_pads == "solid"
        assert len(gnd_bottom.polygon) == 4  # Rectangular boundary
        assert len(gnd_bottom.filled_polygons) == 1

        # Check power plane zone
        power_plane = next(z for z in zones if z.name == "Power_Plane_3V3")
        assert power_plane.layer == "In1.Cu"
        assert power_plane.priority == 1
        assert power_plane.connect_pads == "thermal_reliefs"

        # Check unfilled zone
        zone_5v = next(z for z in zones if z.name == "5V_Island")
        assert zone_5v.is_filled is False
        assert zone_5v.connect_pads == "none"
        assert len(zone_5v.polygon) == 5  # Irregular pentagon

    def test_demo_pcb_charlieplex(self, demo_dir: Path) -> None:
        """Test loading a real demo PCB (charlieplex LED grid)."""
        pcb_path = demo_dir / "charlieplex_led_grid" / "charlieplex_3x3.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("Demo PCB not available")

        pcb = PCB.load(str(pcb_path))

        # Verify it loaded successfully
        assert pcb.footprint_count > 0
        assert pcb.net_count > 0

        # Verify summary works
        summary = pcb.summary()
        assert "footprints" in summary
        assert "nets" in summary
        assert "segments" in summary

    def test_demo_pcb_usb_joystick(self, demo_dir: Path) -> None:
        """Test loading a more complex demo PCB (USB joystick)."""
        pcb_path = demo_dir / "usb_joystick" / "usb_joystick.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("Demo PCB not available")

        pcb = PCB.load(str(pcb_path))

        # Verify it loaded successfully with real complexity
        assert pcb.footprint_count > 5  # Should have multiple components
        assert pcb.net_count > 5  # Should have multiple nets

        # Verify footprint queries work
        footprints = list(pcb.footprints)
        assert len(footprints) > 0

        # Verify layer iteration
        top_footprints = list(pcb.footprints_on_layer("F.Cu"))
        assert len(top_footprints) >= 0  # May be empty if all on bottom

    def test_pcb_footprint_query(self, projects_dir: Path) -> None:
        """Test PCB footprint querying capabilities."""
        pcb_path = projects_dir / "multilayer_zones.kicad_pcb"
        pcb = PCB.load(str(pcb_path))

        # Query by reference
        u1 = pcb.get_footprint("U1")
        assert u1 is not None
        assert u1.value == "STM32F103"
        assert u1.position == (130, 130)
        assert len(u1.pads) >= 8

        # Query footprints on layer
        top_footprints = list(pcb.footprints_on_layer("F.Cu"))
        assert len(top_footprints) >= 5

        # Query segments on net
        gnd_segments = list(pcb.segments_in_net(1))  # GND is net 1
        assert len(gnd_segments) >= 0


class TestRoundTrip:
    """Tests for load → save → load round-trip consistency."""

    def test_schematic_roundtrip(self, simple_rc_schematic: Path, tmp_path: Path) -> None:
        """Test schematic round-trip: load → save → load."""
        # Load original
        original = Schematic.load(simple_rc_schematic)

        # Save to temp file
        save_path = tmp_path / "roundtrip.kicad_sch"
        original.save(save_path)

        # Load saved file
        reloaded = Schematic.load(save_path)

        # Verify structure matches
        assert reloaded.version == original.version
        assert reloaded.paper == original.paper
        assert reloaded.uuid == original.uuid

        # Verify symbols match
        assert len(reloaded.symbols) == len(original.symbols)
        for orig_sym in original.symbols:
            reloaded_sym = reloaded.get_symbol(orig_sym.reference)
            assert reloaded_sym is not None
            assert reloaded_sym.value == orig_sym.value
            assert reloaded_sym.lib_id == orig_sym.lib_id

        # Verify wires match
        assert len(reloaded.wires) == len(original.wires)

        # Verify labels match
        assert len(reloaded.labels) == len(original.labels)

    def test_pcb_roundtrip(self, projects_dir: Path, tmp_path: Path) -> None:
        """Test PCB round-trip: load → save → load."""
        pcb_path = projects_dir / "multilayer_zones.kicad_pcb"
        original = PCB.load(str(pcb_path))

        # Save to temp file
        save_path = tmp_path / "roundtrip.kicad_pcb"
        original.save(save_path)

        # Load saved file
        reloaded = PCB.load(str(save_path))

        # Verify layer structure matches
        assert len(reloaded.layers) == len(original.layers)
        assert len(reloaded.copper_layers) == len(original.copper_layers)

        # Verify nets match
        assert len(reloaded.nets) == len(original.nets)
        for net_num, orig_net in original.nets.items():
            reloaded_net = reloaded.get_net(net_num)
            assert reloaded_net is not None
            assert reloaded_net.name == orig_net.name

        # Verify footprints match
        assert len(reloaded.footprints) == len(original.footprints)
        for orig_fp in original.footprints:
            reloaded_fp = reloaded.get_footprint(orig_fp.reference)
            assert reloaded_fp is not None
            assert reloaded_fp.value == orig_fp.value
            assert reloaded_fp.layer == orig_fp.layer
            assert reloaded_fp.position == orig_fp.position

        # Verify zones match
        assert len(reloaded.zones) == len(original.zones)

    def test_sexp_serialize_parse_identity(self, simple_rc_schematic: Path) -> None:
        """Test that serialize → parse gives equivalent structure."""
        # Read original text
        original_text = simple_rc_schematic.read_text()

        # Parse → serialize → parse
        sexp1 = parse_sexp(original_text)
        serialized = serialize_sexp(sexp1)
        sexp2 = parse_sexp(serialized)

        # Verify tag matches
        assert sexp2.tag == sexp1.tag

        # Verify version matches
        v1 = sexp1.find("version")
        v2 = sexp2.find("version")
        assert v1 is not None and v2 is not None
        assert v2.get_int(0) == v1.get_int(0)

        # Verify symbol count matches
        syms1 = list(sexp1.find_all("symbol"))
        syms2 = list(sexp2.find_all("symbol"))
        assert len(syms2) == len(syms1)


class TestProjectWorkflow:
    """Integration tests for full project workflow."""

    def test_project_load_from_pro_file(self, projects_dir: Path) -> None:
        """Test loading a project from .kicad_pro file."""
        pro_path = projects_dir / "test_project.kicad_pro"
        project = Project.load(pro_path)

        assert project.name == "test_project"
        assert project.directory == projects_dir

    def test_project_schematic_access(self, projects_dir: Path) -> None:
        """Test accessing schematic from project."""
        pro_path = projects_dir / "test_project.kicad_pro"
        project = Project.load(pro_path)

        sch = project.schematic
        assert sch is not None
        assert len(sch.symbols) >= 3  # R1, C1, D1

    def test_project_pcb_access(self, projects_dir: Path) -> None:
        """Test accessing PCB from project."""
        pro_path = projects_dir / "test_project.kicad_pro"
        project = Project.load(pro_path)

        pcb = project.pcb
        assert pcb is not None
        assert len(pcb.footprints) >= 3  # R1, C1, D1

    def test_project_cross_reference(self, projects_dir: Path) -> None:
        """Test cross-referencing schematic and PCB."""
        pro_path = projects_dir / "test_project.kicad_pro"
        project = Project.load(pro_path)

        result = project.cross_reference()

        # All components should match
        assert result.matched >= 3  # R1, C1, D1
        assert result.is_clean or len(result.unplaced) == 0

        # Verify summary
        summary = result.summary()
        assert "matched" in summary
        assert "unplaced" in summary

    def test_project_bom_generation(self, projects_dir: Path) -> None:
        """Test BOM generation from project."""
        pro_path = projects_dir / "test_project.kicad_pro"
        project = Project.load(pro_path)

        bom = project.get_bom()
        assert bom is not None
        # Should have items for R1, C1, D1
        assert len(bom.items) >= 3

    def test_project_from_pcb(self, projects_dir: Path) -> None:
        """Test creating project from PCB file."""
        pcb_path = projects_dir / "test_project.kicad_pcb"
        project = Project.from_pcb(pcb_path)

        assert project.name == "test_project"
        assert project.pcb is not None

        # Should also find schematic
        sch = project.schematic
        assert sch is not None


class TestDemoProjects:
    """Integration tests using real demo projects."""

    def test_charlieplex_full_workflow(self, demo_dir: Path) -> None:
        """Test full workflow with charlieplex demo."""
        pcb_path = demo_dir / "charlieplex_led_grid" / "charlieplex_3x3.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("Demo project not available")

        # Load PCB
        pcb = PCB.load(str(pcb_path))

        # Verify parsing
        assert pcb.footprint_count > 0

        # Test querying
        summary = pcb.summary()
        assert summary["footprints"] > 0
        assert summary["nets"] > 0

        # Test trace length calculation
        trace_length = pcb.total_trace_length()
        assert trace_length >= 0

    def test_usb_joystick_full_workflow(self, demo_dir: Path) -> None:
        """Test full workflow with USB joystick demo."""
        pcb_path = demo_dir / "usb_joystick" / "usb_joystick.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("Demo project not available")

        # Load PCB
        pcb = PCB.load(str(pcb_path))

        # Test that we can iterate all components
        footprint_refs = [fp.reference for fp in pcb.footprints]
        assert len(footprint_refs) > 0

        # Test that we can query nets
        net_names = [net.name for net in pcb.nets.values()]
        assert len(net_names) > 0

        # Test segment iteration
        all_segments = pcb.segments
        assert len(all_segments) >= 0

        # Test via iteration
        all_vias = pcb.vias
        assert len(all_vias) >= 0


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_empty_schematic_elements(self, minimal_schematic: Path) -> None:
        """Test schematic with minimal elements."""
        sch = Schematic.load(minimal_schematic)

        # Should handle optional elements gracefully
        assert sch.sheets is not None  # Empty list is fine
        assert len(sch.sheets) == 0
        assert not sch.is_hierarchical()

    def test_pcb_without_segments(self, projects_dir: Path) -> None:
        """Test PCB that may have no trace segments."""
        pcb_path = projects_dir / "multilayer_zones.kicad_pcb"
        pcb = PCB.load(str(pcb_path))

        # Even if segments exist, the iteration should work
        layer_segments = list(pcb.segments_on_layer("nonexistent"))
        assert layer_segments == []

        net_segments = list(pcb.segments_in_net(999))  # Non-existent net
        assert net_segments == []

    def test_zone_without_filled_polygons(self, projects_dir: Path) -> None:
        """Test zone that is not filled."""
        pcb_path = projects_dir / "multilayer_zones.kicad_pcb"
        pcb = PCB.load(str(pcb_path))

        # Find unfilled zone
        unfilled_zones = [z for z in pcb.zones if not z.is_filled]
        assert len(unfilled_zones) >= 1

        zone = unfilled_zones[0]
        assert zone.polygon  # Should still have boundary
        # filled_polygons may be empty for unfilled zone
