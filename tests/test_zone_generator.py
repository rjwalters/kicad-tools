"""Tests for the ZoneGenerator module."""

import pytest

from kicad_tools.zones import ZoneConfig, ZoneGenerator, ZoneOverlapWarning, parse_power_nets
from kicad_tools.zones.generator import GeneratedZone, _assign_layers_for_pour_nets


class TestParsePowerNets:
    """Tests for power-nets specification parsing."""

    def test_single_net(self):
        """Parse single power net."""
        result = parse_power_nets("GND:B.Cu")
        assert result == [("GND", "B.Cu")]

    def test_multiple_nets(self):
        """Parse multiple power nets."""
        result = parse_power_nets("GND:B.Cu,+3.3V:F.Cu")
        assert result == [("GND", "B.Cu"), ("+3.3V", "F.Cu")]

    def test_with_spaces(self):
        """Parse with spaces around separators."""
        result = parse_power_nets(" GND : B.Cu , +3.3V : F.Cu ")
        assert result == [("GND", "B.Cu"), ("+3.3V", "F.Cu")]

    def test_empty_string(self):
        """Empty string returns empty list."""
        result = parse_power_nets("")
        assert result == []

    def test_whitespace_only(self):
        """Whitespace-only returns empty list."""
        result = parse_power_nets("   ")
        assert result == []

    def test_missing_colon_raises(self):
        """Missing colon raises ValueError."""
        with pytest.raises(ValueError, match="Invalid power net format"):
            parse_power_nets("GND")

    def test_empty_net_name_raises(self):
        """Empty net name raises ValueError."""
        with pytest.raises(ValueError, match="Empty net name"):
            parse_power_nets(":B.Cu")

    def test_empty_layer_raises(self):
        """Empty layer raises ValueError."""
        with pytest.raises(ValueError, match="Empty layer"):
            parse_power_nets("GND:")

    def test_inner_layer(self):
        """Parse inner layer specification."""
        result = parse_power_nets("GND:In1.Cu,+5V:In2.Cu")
        assert result == [("GND", "In1.Cu"), ("+5V", "In2.Cu")]

    def test_special_characters_in_net(self):
        """Net names can contain special characters."""
        result = parse_power_nets("+3.3V:F.Cu,-12V:B.Cu")
        assert result == [("+3.3V", "F.Cu"), ("-12V", "B.Cu")]


class TestZoneConfig:
    """Tests for ZoneConfig dataclass."""

    def test_defaults(self):
        """Default values are set correctly."""
        config = ZoneConfig(net="GND", layer="B.Cu")
        assert config.net == "GND"
        assert config.layer == "B.Cu"
        assert config.priority == 0
        assert config.clearance == 0.3
        assert config.min_thickness == 0.25
        assert config.thermal_gap == 0.3
        assert config.thermal_bridge_width == 0.4
        assert config.boundary is None

    def test_custom_values(self):
        """Custom values are stored correctly."""
        boundary = [(0, 0), (100, 0), (100, 100), (0, 100)]
        config = ZoneConfig(
            net="+3.3V",
            layer="F.Cu",
            priority=2,
            clearance=0.5,
            min_thickness=0.3,
            thermal_gap=0.4,
            thermal_bridge_width=0.5,
            boundary=boundary,
        )
        assert config.net == "+3.3V"
        assert config.layer == "F.Cu"
        assert config.priority == 2
        assert config.clearance == 0.5
        assert config.boundary == boundary


class TestGeneratedZone:
    """Tests for GeneratedZone dataclass."""

    def test_to_sexp_node(self):
        """GeneratedZone generates valid S-expression."""
        config = ZoneConfig(net="GND", layer="B.Cu", priority=1)
        boundary = [(0, 0), (100, 0), (100, 100), (0, 100)]
        zone = GeneratedZone(
            config=config,
            net_number=1,
            boundary=boundary,
            uuid="test-uuid-123",
        )

        sexp = zone.to_sexp_node()
        sexp_str = sexp.to_string()

        # Check key elements are present
        assert "(zone" in sexp_str
        assert "(net 1)" in sexp_str
        assert '(net_name "GND")' in sexp_str
        assert '(layer "B.Cu")' in sexp_str
        assert '(uuid "test-uuid-123")' in sexp_str
        assert "(polygon" in sexp_str
        assert "(priority 1)" in sexp_str


class TestZoneGeneratorUnit:
    """Unit tests for ZoneGenerator (no file I/O)."""

    def test_estimate_board_bounds_no_footprints(self):
        """Board bounds estimation with no footprints returns default."""
        # Create a minimal mock PCB
        from unittest.mock import MagicMock

        mock_pcb = MagicMock()
        mock_pcb.footprints = []
        mock_pcb.zones = []

        gen = ZoneGenerator(mock_pcb, doc=None)
        bounds = gen._estimate_board_bounds()

        # Default bounds
        assert bounds == [(0, 0), (100, 0), (100, 100), (0, 100)]

    def test_estimate_board_bounds_with_footprints(self):
        """Board bounds estimation based on component positions."""
        from unittest.mock import MagicMock

        mock_pcb = MagicMock()

        # Create mock footprints
        fp1 = MagicMock()
        fp1.position = (10.0, 20.0)
        fp2 = MagicMock()
        fp2.position = (50.0, 40.0)

        mock_pcb.footprints = [fp1, fp2]
        mock_pcb.zones = []

        gen = ZoneGenerator(mock_pcb, doc=None)
        bounds = gen._estimate_board_bounds()

        # Bounds should include component positions with padding
        min_x = bounds[0][0]
        min_y = bounds[0][1]
        max_x = bounds[2][0]
        max_y = bounds[2][1]

        assert min_x < 10.0  # Includes padding
        assert min_y < 20.0
        assert max_x > 50.0
        assert max_y > 40.0

    def test_generate_sexp_empty(self):
        """Generate S-expression with no zones returns empty string."""
        from unittest.mock import MagicMock

        mock_pcb = MagicMock()
        mock_pcb.footprints = []
        mock_pcb.zones = []

        gen = ZoneGenerator(mock_pcb, doc=None)
        sexp = gen.generate_sexp()

        assert sexp == ""

    def test_get_statistics_empty(self):
        """Statistics with no zones."""
        from unittest.mock import MagicMock

        mock_pcb = MagicMock()
        mock_pcb.footprints = []
        mock_pcb.zones = []

        gen = ZoneGenerator(mock_pcb, doc=None)
        stats = gen.get_statistics()

        assert stats["zone_count"] == 0
        assert stats["zones"] == []


class TestZoneGeneratorIntegration:
    """Integration tests for ZoneGenerator with real PCB files."""

    @pytest.fixture
    def sample_pcb_path(self, tmp_path):
        """Create a minimal valid PCB file for testing."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_from_pcb(self, sample_pcb_path):
        """Load PCB and create generator."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)
        assert gen.pcb is not None

    def test_add_zone(self, sample_pcb_path):
        """Add a zone to the generator."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        zone = gen.add_zone(
            net="GND",
            layer="B.Cu",
            priority=1,
        )

        assert zone.config.net == "GND"
        assert zone.config.layer == "B.Cu"
        assert zone.net_number == 1
        assert len(gen.zones) == 1

    def test_add_zone_unknown_net_raises(self, sample_pcb_path):
        """Adding zone with unknown net raises ValueError."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        with pytest.raises(ValueError, match="not found"):
            gen.add_zone(net="NONEXISTENT", layer="B.Cu")

    def test_add_ground_plane(self, sample_pcb_path):
        """Add ground plane with convenience method."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        zone = gen.add_ground_plane(layer="B.Cu")

        assert zone.config.net == "GND"
        assert zone.config.layer == "B.Cu"
        assert zone.config.priority == 1  # GND gets priority 1 by default

    def test_add_power_plane(self, sample_pcb_path):
        """Add power plane with convenience method."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        zone = gen.add_power_plane(net="+3.3V", layer="F.Cu")

        assert zone.config.net == "+3.3V"
        assert zone.config.layer == "F.Cu"
        assert zone.config.priority == 0  # Power gets priority 0

    def test_generate_sexp(self, sample_pcb_path):
        """Generate S-expression for zones."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)
        gen.add_zone(net="GND", layer="B.Cu")

        sexp = gen.generate_sexp()

        assert "(zone" in sexp
        assert '(net_name "GND")' in sexp

    def test_save(self, sample_pcb_path, tmp_path):
        """Save PCB with zones."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)
        gen.add_zone(net="GND", layer="B.Cu")

        output_path = tmp_path / "output.kicad_pcb"
        gen.save(output_path)

        assert output_path.exists()

        # Verify zone is in output
        content = output_path.read_text()
        assert "(zone" in content
        assert '(net_name "GND")' in content

    def test_multiple_zones(self, sample_pcb_path):
        """Add multiple zones."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        assert len(gen.zones) == 2

        stats = gen.get_statistics()
        assert stats["zone_count"] == 2

    def test_custom_boundary(self, sample_pcb_path):
        """Add zone with custom boundary."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        custom_boundary = [(10, 10), (40, 10), (40, 40), (10, 40)]
        zone = gen.add_zone(
            net="GND",
            layer="B.Cu",
            boundary=custom_boundary,
        )

        assert zone.boundary == custom_boundary


class TestZoneGeneratorNonzeroOrigin:
    """Tests for ZoneGenerator when board origin is non-zero."""

    @pytest.fixture
    def offset_pcb_path(self, tmp_path):
        """Create a PCB file with board outline at (100,80)."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (gr_rect
    (start 100 80)
    (end 150 110)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""
        pcb_file = tmp_path / "offset.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_board_outline_is_sheet_absolute(self, offset_pcb_path):
        """Zone boundary from board_outline must be sheet-absolute for PCB output.

        get_board_outline() returns board-relative coords, but the zone
        generator must convert back so zone_node writes correct coordinates.
        """
        gen = ZoneGenerator.from_pcb(offset_pcb_path)
        outline = gen.board_outline

        xs = [p[0] for p in outline]
        ys = [p[1] for p in outline]
        # Should be in sheet-absolute: x in [100,150], y in [80,110]
        assert min(xs) == pytest.approx(100.0, abs=0.5)
        assert max(xs) == pytest.approx(150.0, abs=0.5)
        assert min(ys) == pytest.approx(80.0, abs=0.5)
        assert max(ys) == pytest.approx(110.0, abs=0.5)

    def test_add_zone_uses_sheet_absolute_boundary(self, offset_pcb_path):
        """Zone added without explicit boundary uses sheet-absolute outline."""
        gen = ZoneGenerator.from_pcb(offset_pcb_path)
        zone = gen.add_zone(net="GND", layer="B.Cu")

        xs = [p[0] for p in zone.boundary]
        ys = [p[1] for p in zone.boundary]
        assert min(xs) == pytest.approx(100.0, abs=0.5)
        assert max(xs) == pytest.approx(150.0, abs=0.5)
        assert min(ys) == pytest.approx(80.0, abs=0.5)
        assert max(ys) == pytest.approx(110.0, abs=0.5)


class TestZoneOverlapDetection:
    """Tests for overlap detection in ZoneGenerator.add_zone()."""

    @pytest.fixture
    def sample_pcb_path(self, tmp_path):
        """Create a minimal valid PCB file for overlap testing."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "+5V")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_no_warning_different_layers(self, sample_pcb_path):
        """No warning when zones are on different layers."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        assert len(gen.warnings) == 0

    def test_warning_same_layer_same_boundary(self, sample_pcb_path):
        """Warning when two zones share the same layer and boundary."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)
        gen.add_zone(net="+5V", layer="F.Cu", priority=0)

        assert len(gen.warnings) == 1
        w = gen.warnings[0]
        assert w.new_net == "+5V"
        assert w.existing_net == "+3.3V"
        assert w.layer == "F.Cu"
        assert "zero copper" in w.message

    def test_warning_lower_priority_gets_zero_copper(self, sample_pcb_path):
        """Warning identifies that lower-priority zone gets zero copper."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="GND", layer="F.Cu", priority=1)
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        assert len(gen.warnings) == 1
        w = gen.warnings[0]
        assert "new zone will get zero copper" in w.message

    def test_warning_higher_priority_overrides(self, sample_pcb_path):
        """Warning identifies that higher-priority zone overrides existing."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)
        gen.add_zone(net="GND", layer="F.Cu", priority=1)

        assert len(gen.warnings) == 1
        w = gen.warnings[0]
        assert "other zone will get zero copper" in w.message

    def test_no_warning_same_net_same_layer(self, sample_pcb_path):
        """No warning for the same net on the same layer (idempotent re-add)."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="GND", layer="B.Cu", priority=1)
        gen.add_zone(net="GND", layer="B.Cu", priority=1)

        assert len(gen.warnings) == 0

    def test_no_warning_non_overlapping_boundaries(self, sample_pcb_path):
        """No warning when custom boundaries don't overlap."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(
            net="+3.3V",
            layer="F.Cu",
            priority=0,
            boundary=[(0, 0), (20, 0), (20, 50), (0, 50)],
        )
        gen.add_zone(
            net="+5V",
            layer="F.Cu",
            priority=0,
            boundary=[(25, 0), (50, 0), (50, 50), (25, 50)],
        )

        assert len(gen.warnings) == 0

    def test_warning_overlapping_custom_boundaries(self, sample_pcb_path):
        """Warning when custom boundaries overlap on the same layer."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(
            net="+3.3V",
            layer="F.Cu",
            priority=0,
            boundary=[(0, 0), (30, 0), (30, 50), (0, 50)],
        )
        gen.add_zone(
            net="+5V",
            layer="F.Cu",
            priority=0,
            boundary=[(20, 0), (50, 0), (50, 50), (20, 50)],
        )

        assert len(gen.warnings) == 1

    def test_warning_emitted_to_stderr(self, sample_pcb_path, capsys):
        """Overlap warnings are printed to stderr."""
        gen = ZoneGenerator.from_pcb(sample_pcb_path)

        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)
        gen.add_zone(net="+5V", layer="F.Cu", priority=0)

        captured = capsys.readouterr()
        assert "WARNING:" in captured.err
        assert "zero copper" in captured.err


class TestBoundariesOverlap:
    """Tests for the static _boundaries_overlap method."""

    def test_identical_boundaries(self):
        """Identical boundaries overlap."""
        b = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert ZoneGenerator._boundaries_overlap(b, b) is True

    def test_disjoint_boundaries(self):
        """Non-overlapping boundaries do not overlap."""
        a = [(0, 0), (10, 0), (10, 10), (0, 10)]
        b = [(20, 0), (30, 0), (30, 10), (20, 10)]
        assert ZoneGenerator._boundaries_overlap(a, b) is False

    def test_adjacent_boundaries_no_overlap(self):
        """Boundaries sharing an edge do not overlap (exclusive comparison)."""
        a = [(0, 0), (10, 0), (10, 10), (0, 10)]
        b = [(10, 0), (20, 0), (20, 10), (10, 10)]
        assert ZoneGenerator._boundaries_overlap(a, b) is False

    def test_nested_boundaries(self):
        """Smaller boundary inside larger one overlaps."""
        outer = [(0, 0), (100, 0), (100, 100), (0, 100)]
        inner = [(10, 10), (20, 10), (20, 20), (10, 20)]
        assert ZoneGenerator._boundaries_overlap(outer, inner) is True

    def test_empty_boundary(self):
        """Empty boundary does not overlap."""
        b = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert ZoneGenerator._boundaries_overlap([], b) is False
        assert ZoneGenerator._boundaries_overlap(b, []) is False


class TestAssignLayersForPourNets:
    """Tests for _assign_layers_for_pour_nets layer assignment logic."""

    def test_2_layer_ground_on_bcu(self):
        """2-layer board: GND goes on B.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            2,
            [("GND", NetClass.GROUND)],
        )
        assert result == [("GND", "B.Cu", 1)]

    def test_2_layer_power_on_fcu(self):
        """2-layer board: power nets go on F.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            2,
            [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)],
        )
        assert ("GND", "B.Cu", 1) in result
        assert ("+3.3V", "F.Cu", 0) in result

    def test_4_layer_ground_on_in1cu(self):
        """4-layer board: GND goes on In1.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [("GND", NetClass.GROUND)],
        )
        assert result == [("GND", "In1.Cu", 1)]

    def test_4_layer_single_power_on_in2cu(self):
        """4-layer board: single power net goes on In2.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)],
        )
        assert ("GND", "In1.Cu", 1) in result
        assert ("+3.3V", "In2.Cu", 0) in result

    def test_4_layer_multiple_power_nets(self):
        """4-layer board: multiple power nets distributed across layers."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GND", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
                ("+5V", NetClass.POWER),
            ],
        )
        assert ("GND", "In1.Cu", 1) in result
        assert ("+3.3V", "In2.Cu", 0) in result
        assert ("+5V", "F.Cu", 0) in result

    def test_4_layer_three_power_nets(self):
        """4-layer board: three power nets -- first on In2.Cu, rest on F.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GND", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
                ("+5V", NetClass.POWER),
                ("+1.8V", NetClass.POWER),
            ],
        )
        assert ("GND", "In1.Cu", 1) in result
        assert ("+3.3V", "In2.Cu", 0) in result
        # Additional power nets go on F.Cu with increasing priority index
        assert ("+5V", "F.Cu", 0) in result
        assert ("+1.8V", "F.Cu", 1) in result


class TestAutoCreateZones4Layer:
    """Tests for auto_create_zones_for_pour_nets with 4-layer boards."""

    @pytest.fixture
    def four_layer_pcb_path(self, tmp_path):
        """Create a 4-layer PCB file for testing."""
        pcb_content = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "+5V")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
)
"""
        pcb_file = tmp_path / "four_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_4_layer_assigns_inner_layers(self, four_layer_pcb_path):
        """auto_create_zones assigns inner layers for 4-layer boards."""
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        count = auto_create_zones_for_pour_nets(
            four_layer_pcb_path,
            [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)],
        )

        assert count == 2

        # Verify zones were saved correctly
        pcb = PCB.load(str(four_layer_pcb_path))
        zone_layers = {z.net_name: z.layer for z in pcb.zones}

        assert zone_layers["GND"] == "In1.Cu"
        assert zone_layers["+3.3V"] == "In2.Cu"

    def test_4_layer_multiple_power_nets_warns(self, four_layer_pcb_path, capsys):
        """auto_create_zones emits overlap warnings for multiple power on F.Cu."""
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        count = auto_create_zones_for_pour_nets(
            four_layer_pcb_path,
            [
                ("GND", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
                ("+5V", NetClass.POWER),
            ],
        )

        assert count == 3

        # The two power nets should NOT overlap since first goes to In2.Cu
        # and second goes to F.Cu -- no warning expected
        captured = capsys.readouterr()
        assert "WARNING" not in captured.err
