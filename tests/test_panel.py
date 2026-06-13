"""Tests for the panel module -- panelization engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.sexp.parser import SExp

# ---------------------------------------------------------------------------
# Skip if Shapely is not available
# ---------------------------------------------------------------------------
shapely = pytest.importorskip("shapely", reason="Shapely required for panel tests")

from kicad_tools.panel.config import (  # noqa: E402
    MousebiteConfig,
    PanelConfig,
    TabConfig,
    VCutConfig,
)
from kicad_tools.panel.cuts import (  # noqa: E402
    generate_mousebite_holes,
    generate_vcut_lines,
    mousebite_hole_to_sexp,
    vcut_line_to_sexp,
)
from kicad_tools.panel.furniture import (  # noqa: E402
    compute_fiducials,
    compute_tooling_holes,
    fiducial_to_sexp,
    tooling_hole_to_sexp,
)
from kicad_tools.panel.panel import (  # noqa: E402
    Panel,
    _deep_copy_sexp,
    _offset_positions,
    _remap_reference,
    _remap_uuids,
)
from kicad_tools.panel.tabs import (  # noqa: E402
    Tab,
    compute_tabs_between_boards,
    compute_tabs_to_frame,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "projects"
TEST_PCB = FIXTURES_DIR / "test_project.kicad_pcb"


# ---------------------------------------------------------------------------
# Tab computation tests
# ---------------------------------------------------------------------------


class TestTabComputation:
    """Tests for tab placement algorithms."""

    def test_tabs_between_horizontal_boards(self):
        """Tabs between side-by-side boards use vertical orientation."""
        a_bounds = (0, 0, 20, 30)
        b_bounds = (22, 0, 42, 30)
        config = TabConfig(width=3.0, count=3)

        tabs = compute_tabs_between_boards(a_bounds, b_bounds, config, "horizontal")

        assert len(tabs) == 3
        for tab in tabs:
            assert tab.orientation == "vertical"
            # Tab center should be in the gap
            assert 20 < tab.x < 22
            # Tab Y should be within the shared edge
            assert 0 < tab.y < 30

    def test_tabs_between_vertical_boards(self):
        """Tabs between stacked boards use horizontal orientation."""
        a_bounds = (0, 0, 20, 30)
        b_bounds = (0, 32, 20, 62)
        config = TabConfig(width=3.0, count=2)

        tabs = compute_tabs_between_boards(a_bounds, b_bounds, config, "vertical")

        assert len(tabs) == 2
        for tab in tabs:
            assert tab.orientation == "horizontal"
            assert 30 < tab.y < 32
            assert 0 < tab.x < 20

    def test_tabs_with_spacing_override(self):
        """Tab count derived from spacing when spacing is set."""
        a_bounds = (0, 0, 20, 30)
        b_bounds = (22, 0, 42, 30)
        config = TabConfig(width=3.0, count=99, spacing=10.0)

        tabs = compute_tabs_between_boards(a_bounds, b_bounds, config, "horizontal")

        # 30mm edge / 10mm spacing = 3 tabs
        assert len(tabs) == 3

    def test_tabs_to_frame(self):
        """Tabs are generated on all four edges to frame."""
        board_bounds = (10, 10, 30, 40)
        frame_inner = (5, 5, 35, 45)
        config = TabConfig(width=3.0, count=2)

        tabs = compute_tabs_to_frame(board_bounds, frame_inner, config)

        # 4 edges x 2 tabs each = 8 tabs
        assert len(tabs) == 8


# ---------------------------------------------------------------------------
# Tab properties tests
# ---------------------------------------------------------------------------


class TestTabProperties:
    """Tests for Tab bounding box properties."""

    def test_horizontal_tab_bounds(self):
        tab = Tab(x=10, y=20, width=6, height=2, orientation="horizontal")
        assert tab.min_x == pytest.approx(7.0)
        assert tab.max_x == pytest.approx(13.0)
        assert tab.min_y == pytest.approx(19.0)
        assert tab.max_y == pytest.approx(21.0)

    def test_vertical_tab_bounds(self):
        tab = Tab(x=10, y=20, width=6, height=2, orientation="vertical")
        assert tab.min_x == pytest.approx(9.0)
        assert tab.max_x == pytest.approx(11.0)
        assert tab.min_y == pytest.approx(17.0)
        assert tab.max_y == pytest.approx(23.0)


# ---------------------------------------------------------------------------
# Mousebite tests
# ---------------------------------------------------------------------------


class TestMousebites:
    """Tests for mousebite hole generation."""

    def test_mousebite_holes_on_horizontal_tab(self):
        """Holes placed along horizontal center line of a horizontal tab."""
        tab = Tab(x=10, y=5, width=6, height=2, orientation="horizontal")
        config = MousebiteConfig(diameter=0.5, spacing=1.0, offset=0.0)

        holes = generate_mousebite_holes(tab, config)

        assert len(holes) >= 1
        for hole in holes:
            assert hole.y == pytest.approx(5.0)  # On tab center line
            assert hole.diameter == 0.5
            assert tab.min_x <= hole.x <= tab.max_x

    def test_mousebite_holes_on_vertical_tab(self):
        """Holes placed along vertical center line of a vertical tab."""
        tab = Tab(x=5, y=10, width=6, height=2, orientation="vertical")
        config = MousebiteConfig(diameter=0.5, spacing=1.0, offset=0.0)

        holes = generate_mousebite_holes(tab, config)

        assert len(holes) >= 1
        for hole in holes:
            assert hole.x == pytest.approx(5.0)
            assert hole.diameter == 0.5

    def test_mousebite_to_sexp(self):
        """Mousebite hole S-expression has correct structure."""
        from kicad_tools.panel.cuts import MousebiteHole

        hole = MousebiteHole(x=10.5, y=20.3, diameter=0.5)
        sexp = mousebite_hole_to_sexp(hole)

        assert sexp.name == "footprint"
        assert sexp.get_string(0) == "Panel:Mousebite"
        # Should have a pad child
        pad = sexp.find_child("pad")
        assert pad is not None
        # Should have np_thru_hole type
        assert pad.get_string(1) == "np_thru_hole"

    def test_mousebite_correct_layer(self):
        """Mousebite holes use *.Cu and *.Mask layers."""
        from kicad_tools.panel.cuts import MousebiteHole

        hole = MousebiteHole(x=0, y=0, diameter=0.5)
        sexp = mousebite_hole_to_sexp(hole)
        pad = sexp.find_child("pad")
        layers = pad.find_child("layers")
        layer_values = [c.value for c in layers.children if c.is_atom]
        assert "*.Cu" in layer_values
        assert "*.Mask" in layer_values


# ---------------------------------------------------------------------------
# V-cut tests
# ---------------------------------------------------------------------------


class TestVCuts:
    """Tests for V-cut line generation."""

    def test_horizontal_vcuts(self):
        """Horizontal V-cuts span full panel width."""
        panel_bounds = (0, 0, 100, 80)
        positions = [30.0, 50.0]
        config = VCutConfig()

        lines = generate_vcut_lines(panel_bounds, positions, "horizontal", config)

        assert len(lines) == 2
        for line in lines:
            assert line.start_x == pytest.approx(0.0)
            assert line.end_x == pytest.approx(100.0)

    def test_vertical_vcuts(self):
        """Vertical V-cuts span full panel height."""
        panel_bounds = (0, 0, 100, 80)
        positions = [40.0]
        config = VCutConfig()

        lines = generate_vcut_lines(panel_bounds, positions, "vertical", config)

        assert len(lines) == 1
        assert lines[0].start_y == pytest.approx(0.0)
        assert lines[0].end_y == pytest.approx(80.0)

    def test_vcut_to_sexp(self):
        """V-cut line S-expression is a gr_line on Edge.Cuts."""
        from kicad_tools.panel.cuts import VCutLine

        line = VCutLine(start_x=0, start_y=30, end_x=100, end_y=30)
        config = VCutConfig()
        sexp = vcut_line_to_sexp(line, config)

        assert sexp.name == "gr_line"
        layer = sexp.find_child("layer")
        assert layer.get_string(0) == "Edge.Cuts"


# ---------------------------------------------------------------------------
# Furniture tests
# ---------------------------------------------------------------------------


class TestFurniture:
    """Tests for tooling holes and fiducials."""

    def test_three_hole_pattern(self):
        """3-hole pattern places holes at 3 corners."""
        from kicad_tools.panel.config import ToolingHoleConfig

        config = ToolingHoleConfig(diameter=3.0, offset=3.5, pattern=3)
        holes = compute_tooling_holes((0, 0, 100, 80), config)

        assert len(holes) == 3
        for hole in holes:
            assert hole.diameter == 3.0

    def test_four_hole_pattern(self):
        """4-hole pattern places holes at all 4 corners."""
        from kicad_tools.panel.config import ToolingHoleConfig

        config = ToolingHoleConfig(diameter=3.0, offset=3.5, pattern=4)
        holes = compute_tooling_holes((0, 0, 100, 80), config)

        assert len(holes) == 4

    def test_tooling_hole_to_sexp(self):
        """Tooling hole S-expression has NPTH pad."""
        from kicad_tools.panel.furniture import ToolingHole

        hole = ToolingHole(x=5, y=5, diameter=3.0)
        sexp = tooling_hole_to_sexp(hole)

        assert sexp.name == "footprint"
        assert sexp.get_string(0) == "Panel:ToolingHole"

    def test_fiducial_positions(self):
        """Fiducials placed in L-pattern (3 marks)."""
        from kicad_tools.panel.config import FiducialConfig

        config = FiducialConfig(diameter=1.0, mask_margin=2.0, offset=5.0)
        fiducials = compute_fiducials((0, 0, 100, 80), config)

        assert len(fiducials) == 3

    def test_fiducial_to_sexp(self):
        """Fiducial S-expression has SMD pad with solder mask margin."""
        from kicad_tools.panel.furniture import Fiducial

        fid = Fiducial(x=5, y=75, diameter=1.0, mask_margin=2.0)
        sexp = fiducial_to_sexp(fid)

        assert sexp.name == "footprint"
        assert sexp.get_string(0) == "Panel:Fiducial"
        pad = sexp.find_child("pad")
        assert pad.get_string(1) == "smd"


# ---------------------------------------------------------------------------
# S-expression helper tests
# ---------------------------------------------------------------------------


class TestSExpHelpers:
    """Tests for S-expression manipulation utilities."""

    def test_deep_copy_preserves_structure(self):
        """Deep copy creates independent tree with same structure."""
        original = SExp.list(
            "test",
            SExp.list("child", "value"),
            SExp.list("uuid", "original-uuid"),
        )

        copied = _deep_copy_sexp(original)

        assert copied.name == "test"
        assert len(copied.children) == 2
        # Modifying copy should not affect original
        copied.children[0].children[0] = SExp(value="modified")
        assert original.children[0].children[0].value == "value"

    def test_remap_uuids(self):
        """UUID remapping replaces all uuid nodes with fresh values."""
        node = SExp.list(
            "footprint",
            SExp.list("uuid", "old-uuid-1"),
            SExp.list(
                "pad",
                SExp.list("uuid", "old-uuid-2"),
            ),
        )

        _remap_uuids(node)

        uuid1 = node.find_child("uuid").get_string(0)
        uuid2 = node.find_child("pad").find_child("uuid").get_string(0)
        assert uuid1 != "old-uuid-1"
        assert uuid2 != "old-uuid-2"
        assert uuid1 != uuid2

    def test_offset_positions(self):
        """Position offset applies to at, start, end nodes."""
        node = SExp.list(
            "segment",
            SExp.list("start", 10.0, 20.0),
            SExp.list("end", 30.0, 40.0),
        )

        _offset_positions(node, 5.0, 10.0)

        start = node.find_child("start")
        assert start.get_value(0) == pytest.approx(15.0)
        assert start.get_value(1) == pytest.approx(30.0)

        end = node.find_child("end")
        assert end.get_value(0) == pytest.approx(35.0)
        assert end.get_value(1) == pytest.approx(50.0)

    def test_remap_reference(self):
        """Reference designator gets board index prefix."""
        fp = SExp.list(
            "footprint",
            SExp.list("property", "Reference", "R1", SExp.list("at", 0, 0)),
        )

        _remap_reference(fp, 2)

        prop = fp.find_child("property")
        assert prop.get_string(1) == "B2_R1"


# ---------------------------------------------------------------------------
# Net remapping tests
# ---------------------------------------------------------------------------


class TestNetRemapping:
    """Tests for net renaming with board-index prefixes."""

    def test_net_numbers_unique_across_instances(self):
        """Each board instance gets unique net numbers."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2)
        sexp = panel.build()

        # Collect all net definitions
        net_nodes = sexp.find_children("net")
        net_nums = [n.get_value(0) for n in net_nodes]
        # All net numbers should be unique
        assert len(net_nums) == len(set(net_nums))

    def test_net_names_prefixed(self):
        """Net names include board index prefix."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=1, cols=2)
        sexp = panel.build()

        net_nodes = sexp.find_children("net")
        prefixed = [n for n in net_nodes if n.get_string(1) and n.get_string(1).startswith("B")]
        # Should have prefixed nets (all non-zero nets)
        assert len(prefixed) > 0


# ---------------------------------------------------------------------------
# Panel integration tests
# ---------------------------------------------------------------------------


class TestPanelIntegration:
    """Integration tests for the full panel builder."""

    def test_panel_grid_layout(self):
        """Panel places N copies at correct grid positions."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=3, spacing=2.0)

        assert panel.board_count == 6
        instances = panel.instances
        assert len(instances) == 6

        # Check grid positions
        for inst in instances:
            assert inst.row < 2
            assert inst.col < 3

    def test_panel_tabs_generated(self):
        """Tabs are generated between adjacent boards."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2, spacing=2.0)
        panel.make_tabs(width=3.0, count=2)

        # 2x2 grid: 2 horizontal gaps + 2 vertical gaps = 4 gaps
        # 2 tabs per gap = 8 tabs
        assert len(panel.tabs) == 8

    def test_panel_build_produces_valid_sexp(self):
        """Built panel is a valid kicad_pcb S-expression."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2, spacing=2.0)
        panel.make_tabs(width=3.0, count=2)
        panel.make_mousebites(diameter=0.5, spacing=0.8)
        sexp = panel.build()

        assert sexp.name == "kicad_pcb"
        # Should have version, layers, etc.
        assert sexp.find_child("version") is not None
        assert sexp.find_child("layers") is not None

    def test_panel_round_trip(self, tmp_path):
        """Panel can be saved and the output file exists."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        output = tmp_path / "panel_output.kicad_pcb"
        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2, spacing=2.0)
        panel.make_tabs(width=3.0, count=2)
        panel.make_mousebites(diameter=0.5, spacing=0.8)
        result = panel.save(output)

        assert result == output
        assert output.exists()
        content = output.read_text()
        assert content.startswith("(kicad_pcb")

    def test_panel_with_vcuts(self, tmp_path):
        """Panel with V-cuts generates gr_line on Edge.Cuts."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2, spacing=2.0)
        panel.make_tabs(width=3.0, count=2)
        panel.make_vcuts()
        sexp = panel.build()

        # Should have gr_line nodes on Edge.Cuts
        gr_lines = sexp.find_children("gr_line")
        edge_cuts = [
            l
            for l in gr_lines
            if l.find_child("layer") and l.find_child("layer").get_string(0) == "Edge.Cuts"
        ]
        assert len(edge_cuts) > 0

    def test_panel_with_frame(self, tmp_path):
        """Panel with frame generates inner and outer outline."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2, spacing=2.0)
        panel.make_frame(width=5.0, space=2.0)
        panel.make_tabs(width=3.0, count=2)
        panel.make_mousebites()
        sexp = panel.build()

        gr_lines = sexp.find_children("gr_line")
        edge_cuts = [
            l
            for l in gr_lines
            if l.find_child("layer") and l.find_child("layer").get_string(0) == "Edge.Cuts"
        ]
        # Frame adds 8 lines (4 outer + 4 inner) + board outlines + tab lines
        assert len(edge_cuts) >= 8

    def test_panel_from_config(self, tmp_path):
        """Panel.from_config() produces same result as manual steps."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        config = PanelConfig(rows=2, cols=2, spacing=2.0)
        panel = Panel.from_config(TEST_PCB, config)
        output = tmp_path / "panel_config.kicad_pcb"
        panel.save(output)

        assert output.exists()
        assert panel.board_count == 4

    def test_single_board_panel(self, tmp_path):
        """1x1 panel is a valid degenerate case."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=1, cols=1, spacing=0)
        panel.make_tabs(width=3.0, count=0)
        sexp = panel.build()

        assert sexp.name == "kicad_pcb"
        assert panel.board_count == 1

    def test_panel_uuid_uniqueness(self):
        """All UUIDs in the panel are unique."""
        if not TEST_PCB.exists():
            pytest.skip("Test PCB fixture not found")

        panel = Panel()
        panel.append_board(TEST_PCB, rows=2, cols=2)
        panel.make_tabs(width=3.0, count=2)
        panel.make_mousebites()
        sexp = panel.build()

        uuids: list[str] = []
        _collect_uuids(sexp, uuids)
        assert len(uuids) == len(set(uuids)), (
            f"Found {len(uuids) - len(set(uuids))} duplicate UUIDs"
        )


def _collect_uuids(node: SExp, uuids: list[str]) -> None:
    """Recursively collect all UUID values from an S-expression tree."""
    if node.name == "uuid" and node.children:
        val = node.get_string(0)
        if val:
            uuids.append(val)
    for child in node.children:
        if not child.is_atom:
            _collect_uuids(child, uuids)
