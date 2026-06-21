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
        # Issue #3729: zones default to *zone-level thermal relief* (empty
        # mode).  The selective per-pad policy forces solid only on pads that
        # cannot host 2 spokes, so larger pads keep thermal relief.
        assert config.pad_connection == ""
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

    def test_to_sexp_node_thermal_relief_default(self):
        """Issue #3729: generated zones default to zone-level thermal relief.

        The selective policy keeps thermal relief at the zone level (no
        ``connect_pads`` mode token) and forces solid only on the pads that
        cannot host 2 spokes via per-pad ``(zone_connect 2)`` overrides, so
        the generated zone emits a bare ``(connect_pads (clearance ...))``.
        """
        config = ZoneConfig(net="GND", layer="B.Cu", priority=1)
        zone = GeneratedZone(
            config=config,
            net_number=1,
            boundary=[(0, 0), (100, 0), (100, 100), (0, 100)],
            uuid="test-uuid",
        )
        sexp_str = zone.to_sexp_node().to_string()
        assert "(connect_pads (clearance" in sexp_str
        assert "connect_pads yes" not in sexp_str

    def test_to_sexp_node_yes_override(self):
        """An explicit ``pad_connection="yes"`` still writes the blanket-solid mode."""
        config = ZoneConfig(net="GND", layer="B.Cu", priority=1, pad_connection="yes")
        zone = GeneratedZone(
            config=config,
            net_number=1,
            boundary=[(0, 0), (100, 0), (100, 100), (0, 100)],
            uuid="test-uuid",
        )
        sexp_str = zone.to_sexp_node().to_string()
        assert "(connect_pads yes (clearance" in sexp_str


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


class TestZoneOverlapNonzeroOrigin:
    """Tests for overlap detection on PCBs with non-zero board origin.

    Regression coverage for the mixed-coordinate-space bug introduced by
    PR #2753: ``board_outline`` is sheet-absolute (PCB-output frame), but
    ``Zone.polygon`` is board-relative after loading.  ``_check_overlap``
    must reconcile the two frames before running the AABB intersection
    test, otherwise overlap detection silently fails on every non-zero-
    origin board (which is every demo board in this repo).
    """

    @pytest.fixture
    def offset_pcb_with_existing_zone(self, tmp_path):
        """Create a PCB at origin (100, 80) with one pre-existing GND zone.

        The zone polygon is written in sheet-absolute coordinates (KiCad's
        on-disk convention).  After PCB.load, ``Zone.polygon`` will be
        board-relative, while ``board_outline`` (consumed by
        ``ZoneGenerator``) will be re-converted to sheet-absolute for
        PCB output.
        """
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
  (net 2 "+3.3V")
  (net 3 "+5V")
  (gr_rect
    (start 100 80)
    (end 150 110)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
  (zone
    (net 1)
    (net_name "GND")
    (layer "B.Cu")
    (uuid "existing-zone-uuid")
    (hatch edge 0.5)
    (priority 1)
    (connect_pads (clearance 0.3))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.4))
    (polygon (pts (xy 100.3 80.3) (xy 149.7 80.3) (xy 149.7 109.7) (xy 100.3 109.7)))
  )
)
"""
        pcb_file = tmp_path / "offset_with_zone.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    @pytest.fixture
    def offset_pcb_no_zone(self, tmp_path):
        """Create a PCB at origin (100, 80) with no pre-existing zones."""
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
  (net 2 "+3.3V")
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
        pcb_file = tmp_path / "offset_no_zone.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def test_existing_zone_polygon_is_board_relative(self, offset_pcb_with_existing_zone):
        """Confirm the PCB.load invariant: Zone.polygon is board-relative.

        This is a pre-condition for the regression we are guarding against;
        if this assumption changes upstream the overlap fix may become
        unnecessary (or wrong) and these tests should be revisited.
        """
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)
        assert len(gen.pcb.zones) == 1
        existing = gen.pcb.zones[0]

        # Origin should be (100, 80)
        assert gen.pcb.board_origin == (100.0, 80.0)

        # Polygon should be board-relative (~ [0..50] x [0..30])
        xs = [p[0] for p in existing.polygon]
        ys = [p[1] for p in existing.polygon]
        assert min(xs) == pytest.approx(0.3, abs=0.5)
        assert max(xs) == pytest.approx(49.7, abs=0.5)
        assert min(ys) == pytest.approx(0.3, abs=0.5)
        assert max(ys) == pytest.approx(29.7, abs=0.5)

    def test_overlap_detected_with_nonzero_origin(self, offset_pcb_with_existing_zone):
        """Overlap warning fires when new zone overlaps existing on non-zero origin.

        Regression for PR #2753: before the fix, ``_check_overlap``
        compared sheet-absolute ``boundary`` against board-relative
        ``existing.polygon`` and the AABBs were offset by the board
        origin, so the overlap was silently missed.
        """
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)

        # Add a +3.3V zone on B.Cu, which fully overlaps the existing GND
        # zone on B.Cu.  Use default boundary (==board_outline, sheet-abs).
        gen.add_zone(net="+3.3V", layer="B.Cu", priority=0)

        assert len(gen.warnings) == 1
        w = gen.warnings[0]
        assert isinstance(w, ZoneOverlapWarning)
        assert w.new_net == "+3.3V"
        assert w.existing_net == "GND"
        assert w.layer == "B.Cu"
        # Lower priority new zone => "new zone will get zero copper"
        assert "new zone will get zero copper" in w.message

    def test_overlap_higher_priority_overrides_existing(self, offset_pcb_with_existing_zone):
        """Higher-priority new zone reports the existing-zone-loses warning."""
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)

        # New zone at priority 2 > existing priority 1
        gen.add_zone(net="+5V", layer="B.Cu", priority=2)

        assert len(gen.warnings) == 1
        w = gen.warnings[0]
        assert "existing zone will get zero copper" in w.message
        assert w.existing_net == "GND"

    def test_no_overlap_warning_on_different_layer(self, offset_pcb_with_existing_zone):
        """No overlap warning when new zone is on a different layer."""
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)

        # Existing zone is on B.Cu; add a different zone on F.Cu
        gen.add_zone(net="+3.3V", layer="F.Cu", priority=0)

        assert len(gen.warnings) == 0

    def test_no_overlap_warning_same_net_same_layer(self, offset_pcb_with_existing_zone):
        """No overlap warning when re-adding the same net on the same layer."""
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)

        gen.add_zone(net="GND", layer="B.Cu", priority=1)

        assert len(gen.warnings) == 0

    def test_no_overlap_warning_disjoint_custom_boundary(self, offset_pcb_with_existing_zone):
        """No overlap when caller supplies a sheet-absolute boundary disjoint from the existing zone.

        Existing zone covers (100,80) -> (150,110) in sheet-absolute.
        Supply a boundary far away (e.g. 200,200 -> 250,250) and expect
        no warning.  This guards against false positives caused by an
        over-zealous origin shift.
        """
        gen = ZoneGenerator.from_pcb(offset_pcb_with_existing_zone)

        far_away = [(200, 200), (250, 200), (250, 250), (200, 250)]
        gen.add_zone(
            net="+3.3V",
            layer="B.Cu",
            priority=0,
            boundary=far_away,
        )

        assert len(gen.warnings) == 0

    def test_no_existing_zones_no_warning(self, offset_pcb_no_zone):
        """Sanity check: non-zero origin PCB with no existing zones produces no warning."""
        gen = ZoneGenerator.from_pcb(offset_pcb_no_zone)
        gen.add_zone(net="GND", layer="B.Cu", priority=1)

        assert len(gen.warnings) == 0


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
        """2-layer board: single power net goes on F.Cu with priority 1."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            2,
            [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)],
        )
        assert ("GND", "B.Cu", 1) in result
        assert ("+3.3V", "F.Cu", 1) in result

    def test_2_layer_multiple_power_nets(self):
        """2-layer board: multiple power nets get descending priorities on F.Cu."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            2,
            [
                ("GND", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
                ("+5V", NetClass.POWER),
                ("+1.8V", NetClass.POWER),
            ],
        )
        assert ("GND", "B.Cu", 1) in result
        # 3 power nets get descending priorities: 3, 2, 1
        assert ("+3.3V", "F.Cu", 3) in result
        assert ("+5V", "F.Cu", 2) in result
        assert ("+1.8V", "F.Cu", 1) in result
        # All priorities must be distinct
        fcu_priorities = [p for _, l, p in result if l == "F.Cu"]
        assert len(fcu_priorities) == len(set(fcu_priorities))

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
        assert ("+5V", "F.Cu", 1) in result

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
        # Additional power nets go on F.Cu with non-zero priorities
        assert ("+5V", "F.Cu", 1) in result
        assert ("+1.8V", "F.Cu", 2) in result

    # ------------------------------------------------------------------
    # Split-ground tests (issue #2593): when there are multiple distinct
    # GROUND-class nets, each must get a distinct (layer, priority) so
    # one ground does not silently override another to zero copper.
    # ------------------------------------------------------------------

    def test_4_layer_split_ground_basic(self):
        """4-layer + 2 grounds: split across In1.Cu and In2.Cu (issue #2593)."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [("GND", NetClass.GROUND), ("GNDA", NetClass.GROUND)],
        )
        # Canonical "GND" goes to In1.Cu
        assert ("GND", "In1.Cu", 1) in result
        # Other ground goes to the second inner layer
        assert ("GNDA", "In2.Cu", 1) in result
        # Each (layer, priority) pair is distinct across all assignments
        layer_priority_pairs = [(layer, prio) for _, layer, prio in result]
        assert len(layer_priority_pairs) == len(set(layer_priority_pairs))

    def test_4_layer_split_ground_demotes_power(self):
        """4-layer + 2 grounds + 1 power: power demoted to F.Cu (issue #2593)."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GND", NetClass.GROUND),
                ("GNDA", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
            ],
        )
        assert ("GND", "In1.Cu", 1) in result
        assert ("GNDA", "In2.Cu", 1) in result
        # Power must NOT land on In2.Cu (reserved for the second ground).
        assert ("+3.3V", "F.Cu", 0) in result
        assert ("+3.3V", "In2.Cu", 0) not in result

    def test_4_layer_split_ground_demotes_multiple_power(self):
        """4-layer + 2 grounds + N power: all power on F.Cu with distinct priorities."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GND", NetClass.GROUND),
                ("GNDA", NetClass.GROUND),
                ("+3.3V", NetClass.POWER),
                ("+5V", NetClass.POWER),
            ],
        )
        assert ("GND", "In1.Cu", 1) in result
        assert ("GNDA", "In2.Cu", 1) in result
        # Both power nets on F.Cu with distinct, non-zero priorities
        fcu_assignments = [(n, p) for n, l, p in result if l == "F.Cu"]
        fcu_nets = {n for n, _ in fcu_assignments}
        assert fcu_nets == {"+3.3V", "+5V"}
        fcu_priorities = [p for _, p in fcu_assignments]
        assert len(fcu_priorities) == len(set(fcu_priorities))
        # No power net got assigned to In2.Cu
        in2_nets = {n for n, l, _ in result if l == "In2.Cu"}
        assert "+3.3V" not in in2_nets
        assert "+5V" not in in2_nets

    def test_4_layer_split_ground_three_grounds(self, capsys):
        """4-layer + 3 grounds: extras spill to B.Cu and a warning is emitted."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GND", NetClass.GROUND),
                ("GNDA", NetClass.GROUND),
                ("GNDD", NetClass.GROUND),
            ],
        )
        # Canonical "GND" first on In1.Cu, second alphabetical -> In2.Cu
        assert ("GND", "In1.Cu", 1) in result
        assert ("GNDA", "In2.Cu", 1) in result
        # Third ground spills to B.Cu with a non-zero priority distinct
        # from any other ground's (layer, priority).
        gndd_entries = [(l, p) for n, l, p in result if n == "GNDD"]
        assert len(gndd_entries) == 1
        assert gndd_entries[0][0] == "B.Cu"
        # Every ground net has a distinct (layer, priority) pair
        ground_lp = [(l, p) for n, l, p in result if n in {"GND", "GNDA", "GNDD"}]
        assert len(ground_lp) == len(set(ground_lp))
        # A warning was printed to stderr about >2 ground domains
        captured = capsys.readouterr()
        assert "more than 2 ground domains" in captured.err.lower() or (
            "manual stackup" in captured.err.lower()
        )

    def test_2_layer_split_ground_distinct_priorities(self):
        """2-layer + 2 grounds: both on B.Cu with distinct priorities (issue #2593)."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            2,
            [("GNDA", NetClass.GROUND), ("GNDD", NetClass.GROUND)],
        )
        # Both grounds on B.Cu
        bcu_assignments = [(n, p) for n, l, p in result if l == "B.Cu"]
        bcu_nets = {n for n, _ in bcu_assignments}
        assert bcu_nets == {"GNDA", "GNDD"}
        # Distinct priorities so no zone is silently overridden
        bcu_priorities = [p for _, p in bcu_assignments]
        assert len(bcu_priorities) == len(set(bcu_priorities))

    def test_4_layer_split_ground_canonical_gnd_picks_in1cu(self):
        """4-layer split-ground: literal 'GND' is preferred for In1.Cu."""
        from kicad_tools.router.net_class import NetClass

        # Provide the inputs in a non-canonical order to ensure the
        # selection logic, not the input order, decides In1.Cu.
        result = _assign_layers_for_pour_nets(
            4,
            [
                ("GNDA", NetClass.GROUND),
                ("GNDD", NetClass.GROUND),
                ("GND", NetClass.GROUND),
            ],
        )
        assert ("GND", "In1.Cu", 1) in result

    def test_4_layer_split_ground_no_canonical_uses_alpha(self):
        """4-layer split-ground without 'GND': alphabetical decides In1.Cu."""
        from kicad_tools.router.net_class import NetClass

        # Reverse the input order; alphabetical should still put GNDA on In1.
        result = _assign_layers_for_pour_nets(
            4,
            [("GNDD", NetClass.GROUND), ("GNDA", NetClass.GROUND)],
        )
        assert ("GNDA", "In1.Cu", 1) in result
        assert ("GNDD", "In2.Cu", 1) in result

    def test_4_layer_split_ground_logs_to_stderr(self, capsys):
        """4-layer split-ground emits an info line naming the detected grounds."""
        from kicad_tools.router.net_class import NetClass

        _assign_layers_for_pour_nets(
            4,
            [("GNDA", NetClass.GROUND), ("GNDD", NetClass.GROUND)],
        )
        captured = capsys.readouterr()
        assert "split-ground detected" in captured.err.lower()
        # Both ground names appear in the message
        assert "GNDA" in captured.err
        assert "GNDD" in captured.err

    def test_4_layer_split_ground_no_overlap_between_grounds(self):
        """Critical regression test for #2593: no two ground nets get the
        same (layer, priority) — that's the exact condition that produced
        the 'will get zero copper' fill warning on chorus-test-revA."""
        from kicad_tools.router.net_class import NetClass

        result = _assign_layers_for_pour_nets(
            4,
            [("GNDA", NetClass.GROUND), ("GNDD", NetClass.GROUND)],
        )
        ground_lp = [(l, p) for n, l, p in result if n in {"GNDA", "GNDD"}]
        assert len(ground_lp) == 2
        assert len(set(ground_lp)) == 2  # all distinct


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


class TestAutoCreateZonesReplaceExisting:
    """Issue #3818: ``replace_existing`` makes pour creation idempotent.

    A prior pipeline step (``kct route``'s internal auto-pour) can leave a
    zone per pour net.  The additive default would stack a SECOND,
    overlapping same-net same-layer zone on top -- and KiCad's fill
    resolver awards the shared region to one duplicate non-deterministically,
    leaving the other with ZERO ``filled_polygon`` regions (the "dead pour"
    the copper-union audit flags).  ``replace_existing=True`` drops the
    pre-existing zones first so the board ends with exactly one zone per net.
    """

    @pytest.fixture
    def four_layer_pcb_path(self, tmp_path):
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
        pcb_file = tmp_path / "four_layer.kicad_pcb"
        pcb_file.write_text(pcb_content)
        return pcb_file

    def _zone_count_by_net(self, pcb_path):
        import re

        text = pcb_path.read_text()
        counts: dict[str, int] = {}
        idxs = [m.start() for m in re.finditer(r"\(zone\b", text)]
        idxs.append(len(text))
        for a, b in zip(idxs[:-1], idxs[1:], strict=True):
            seg = text[a:b]
            m = re.search(r'\(net_name "([^"]*)"\)', seg) or re.search(r'\(net "([^"]*)"\)', seg)
            if m:
                counts[m.group(1)] = counts.get(m.group(1), 0) + 1
        return counts

    def test_default_is_additive(self, four_layer_pcb_path):
        """Without the flag, a second call STACKS a duplicate zone (legacy)."""
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        decl = [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)]
        auto_create_zones_for_pour_nets(four_layer_pcb_path, decl)
        auto_create_zones_for_pour_nets(four_layer_pcb_path, decl)

        counts = self._zone_count_by_net(four_layer_pcb_path)
        assert counts == {"GND": 2, "+3.3V": 2}

    def test_replace_existing_is_idempotent(self, four_layer_pcb_path):
        """With the flag, a second call REPLACES so there is one zone per net."""
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        decl = [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)]
        auto_create_zones_for_pour_nets(four_layer_pcb_path, decl)
        count = auto_create_zones_for_pour_nets(four_layer_pcb_path, decl, replace_existing=True)

        assert count == 2
        counts = self._zone_count_by_net(four_layer_pcb_path)
        assert counts == {"GND": 1, "+3.3V": 1}

    def test_replace_existing_only_touches_listed_nets(self, four_layer_pcb_path):
        """A foreign-net zone already on the board is left untouched."""
        from kicad_tools.router.net_class import NetClass
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        # Seed a GND + +3.3V pour, then re-assert only GND.
        auto_create_zones_for_pour_nets(
            four_layer_pcb_path,
            [("GND", NetClass.GROUND), ("+3.3V", NetClass.POWER)],
        )
        auto_create_zones_for_pour_nets(
            four_layer_pcb_path,
            [("GND", NetClass.GROUND)],
            replace_existing=True,
        )

        counts = self._zone_count_by_net(four_layer_pcb_path)
        # GND was replaced (still 1); +3.3V was not in the list, so its
        # pre-existing zone is preserved.
        assert counts.get("GND") == 1
        assert counts.get("+3.3V") == 1
