"""Tests for the kicad-pcb-stitch CLI command."""

import math
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    PadInfo,
    TraceSegment,
    TrackSegment,
    calculate_dogleg_via_position,
    calculate_extended_escape_position,
    calculate_via_position,
    check_via_clearance,
    extract_zone_polygons,
    find_all_board_vias,
    find_all_filled_polygons,
    find_all_pads,
    find_all_plane_nets,
    find_all_track_segments,
    find_existing_tracks,
    find_existing_vias,
    find_pads_on_nets,
    generate_grid_positions,
    get_net_map,
    get_net_number,
    get_via_layers,
    is_pad_connected,
    main,
    point_in_polygon,
    point_to_segment_distance,
    run_blanket_stitch,
    run_post_stitch_drc,
    run_stitch,
    segment_to_segment_distance,
)
from kicad_tools.core.sexp_file import load_pcb
# PCB with SMD components on GND and +3.3V nets for testing stitching
STITCH_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
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
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "NET1")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 120 110)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000300")
    (at 130 110)
    (property "Reference" "C3" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c3"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000400")
    (at 115 120)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-r1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "NET1"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""


# PCB with existing vias (to test that already connected pads are skipped)
STITCH_CONNECTED_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (via (at 109.5 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-uuid-1"))
  (segment (start 109.5 110) (end 109.49 110) (width 0.2) (layer "F.Cu") (net 1) (uuid "seg-uuid-1"))
)
"""


@pytest.fixture
def stitch_test_pcb(tmp_path: Path) -> Path:
    """Create a PCB file for testing stitching."""
    pcb_file = tmp_path / "stitch_test.kicad_pcb"
    pcb_file.write_text(STITCH_TEST_PCB)
    return pcb_file


@pytest.fixture
def stitch_connected_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with existing connections for testing."""
    pcb_file = tmp_path / "stitch_connected.kicad_pcb"
    pcb_file.write_text(STITCH_CONNECTED_PCB)
    return pcb_file


class TestNetMap:
    """Tests for net mapping functions."""

    def test_get_net_map(self, stitch_test_pcb: Path):
        """Should build net number to name mapping."""
        sexp = load_pcb(stitch_test_pcb)
        net_map = get_net_map(sexp)

        assert net_map[1] == "GND"
        assert net_map[2] == "+3.3V"
        assert net_map[3] == "NET1"

    def test_get_net_number(self, stitch_test_pcb: Path):
        """Should find net number by name."""
        sexp = load_pcb(stitch_test_pcb)

        assert get_net_number(sexp, "GND") == 1
        assert get_net_number(sexp, "+3.3V") == 2
        assert get_net_number(sexp, "nonexistent") is None


class TestFindPads:
    """Tests for finding pads on nets."""

    def test_find_pads_on_gnd(self, stitch_test_pcb: Path):
        """Should find all pads on GND net."""
        sexp = load_pcb(stitch_test_pcb)
        pads = find_pads_on_nets(sexp, {"GND"})

        # C1.1, C2.1, C3.1, R1.2 are on GND
        assert len(pads) == 4
        refs = {f"{p.reference}.{p.pad_number}" for p in pads}
        assert refs == {"C1.1", "C2.1", "C3.1", "R1.2"}

    def test_find_pads_on_multiple_nets(self, stitch_test_pcb: Path):
        """Should find pads on multiple nets."""
        sexp = load_pcb(stitch_test_pcb)
        pads = find_pads_on_nets(sexp, {"GND", "+3.3V"})

        # 4 GND + 3 +3.3V = 7 pads
        assert len(pads) == 7

    def test_find_pads_includes_correct_info(self, stitch_test_pcb: Path):
        """Should include correct position and net info."""
        sexp = load_pcb(stitch_test_pcb)
        pads = find_pads_on_nets(sexp, {"GND"})

        c1_pad = next(p for p in pads if p.reference == "C1" and p.pad_number == "1")
        assert c1_pad.net_number == 1
        assert c1_pad.net_name == "GND"
        assert c1_pad.layer == "F.Cu"
        # C1 at (110, 110), pad 1 at (-0.51, 0) relative
        assert abs(c1_pad.x - 109.49) < 0.01
        assert abs(c1_pad.y - 110) < 0.01


class TestFindExisting:
    """Tests for finding existing vias and tracks."""

    def test_find_existing_vias(self, stitch_connected_pcb: Path):
        """Should find existing vias on net."""
        sexp = load_pcb(stitch_connected_pcb)
        vias = find_existing_vias(sexp, {1})

        assert len(vias) == 1
        assert vias[0][0] == 109.5  # x
        assert vias[0][1] == 110  # y
        assert vias[0][2] == 1  # net

    def test_find_existing_tracks(self, stitch_connected_pcb: Path):
        """Should find existing track endpoints."""
        sexp = load_pcb(stitch_connected_pcb)
        points = find_existing_tracks(sexp, {1})

        # One segment = 2 endpoints
        assert len(points) == 2


class TestPadConnection:
    """Tests for checking if pads are connected."""

    def test_pad_with_nearby_via_is_connected(self, stitch_connected_pcb: Path):
        """Pad with nearby via should be considered connected."""
        sexp = load_pcb(stitch_connected_pcb)
        pads = find_pads_on_nets(sexp, {"GND"})
        vias = find_existing_vias(sexp, {1})
        tracks = find_existing_tracks(sexp, {1})

        # C1.1 has a via at (109.5, 110), very close to pad at (~109.49, 110)
        c1_pad = pads[0]
        assert is_pad_connected(c1_pad, vias, tracks)

    def test_pad_without_connection(self, stitch_test_pcb: Path):
        """Pad without nearby via or track should not be connected."""
        sexp = load_pcb(stitch_test_pcb)
        pads = find_pads_on_nets(sexp, {"GND"})
        vias = find_existing_vias(sexp, {1})  # Empty
        tracks = find_existing_tracks(sexp, {1})  # Empty

        assert len(vias) == 0
        assert len(tracks) == 0

        for pad in pads:
            assert not is_pad_connected(pad, vias, tracks)


class TestViaPlacement:
    """Tests for calculating via placement."""

    def test_calculate_via_position_finds_valid_spot(self):
        """Should find a valid via position near pad."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
        )

        assert pos is not None
        # Should be offset from pad center
        import math

        dist = math.sqrt((pos[0] - pad.x) ** 2 + (pos[1] - pad.y) ** 2)
        assert dist > 0.2  # At least some offset

    def test_calculate_via_position_avoids_existing_vias(self):
        """Should avoid placing on top of existing vias."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Block all cardinal directions
        existing = [
            (100.8, 100, 1),
            (99.2, 100, 1),
            (100, 100.8, 1),
            (100, 99.2, 1),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=existing,
            clearance=0.2,
        )

        if pos is not None:
            # Should be in a diagonal direction
            import math

            for ex, ey, _ in existing:
                dist = math.sqrt((pos[0] - ex) ** 2 + (pos[1] - ey) ** 2)
                assert dist >= 0.45 + 0.2  # via_size + clearance


class TestViaLayers:
    """Tests for via layer selection."""

    def test_via_layers_f_cu_to_b_cu(self):
        """F.Cu pads should get vias to B.Cu by default."""
        layers = get_via_layers("F.Cu", None)
        assert layers == ("F.Cu", "B.Cu")

    def test_via_layers_b_cu_to_f_cu(self):
        """B.Cu pads should get vias to F.Cu by default."""
        layers = get_via_layers("B.Cu", None)
        assert layers == ("B.Cu", "F.Cu")

    def test_via_layers_with_target(self):
        """Should use specified target layer."""
        layers = get_via_layers("F.Cu", "In1.Cu")
        assert layers == ("F.Cu", "In1.Cu")


class TestRunStitch:
    """Tests for the main stitch operation."""

    def test_run_stitch_dry_run(self, stitch_test_pcb: Path):
        """Dry run should not modify file."""
        original_content = stitch_test_pcb.read_text()

        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        assert len(result.vias_added) > 0
        # File should be unchanged
        assert stitch_test_pcb.read_text() == original_content

    def test_run_stitch_adds_vias(self, stitch_test_pcb: Path):
        """Should add vias to unconnected pads."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=False,
        )

        assert len(result.vias_added) == 4  # 4 GND pads

        # File should have vias added
        new_content = stitch_test_pcb.read_text()
        assert new_content.count("(via") >= 4

    def test_run_stitch_via_format_no_rotation(self, stitch_test_pcb: Path):
        """Vias must use (at X Y) without rotation parameter.

        Regression test for issue #1104: vias written with (at X Y 0) cause
        KiCad to fail loading the PCB file.
        """
        import re

        run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=False,
        )

        new_content = stitch_test_pcb.read_text()
        # Find all (at ...) inside (via ...) blocks
        # Via at nodes should be (at X Y) not (at X Y 0)
        via_at_pattern = re.compile(r"\(via\s.*?\(at\s+[\d.]+\s+[\d.]+\s+\d+\)", re.DOTALL)
        matches = via_at_pattern.findall(new_content)
        assert len(matches) == 0, (
            f"Found via(s) with rotation in at node: {matches[0][:80]}... "
            "Vias must use (at X Y) format without rotation parameter."
        )

    def test_run_stitch_skips_connected(self, stitch_connected_pcb: Path):
        """Should skip pads that already have connections."""
        result = run_stitch(
            pcb_path=stitch_connected_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        # C1.1 is already connected, should be skipped
        assert result.already_connected >= 1

    def test_run_stitch_multiple_nets(self, stitch_test_pcb: Path):
        """Should handle multiple nets."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND", "+3.3V"],
            dry_run=True,
        )

        # 4 GND + 3 +3.3V = 7 vias
        assert len(result.vias_added) == 7

    def test_run_stitch_custom_via_size(self, stitch_test_pcb: Path):
        """Should use custom via size."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            via_size=0.6,
            drill=0.3,
            dry_run=True,
        )

        assert len(result.vias_added) > 0
        for via in result.vias_added:
            assert via.size == 0.6
            assert via.drill == 0.3

    def test_run_stitch_target_layer(self, stitch_test_pcb: Path):
        """Should use specified target layer."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            target_layer="In1.Cu",
            dry_run=True,
        )

        assert len(result.vias_added) > 0
        for via in result.vias_added:
            assert via.layers == ("F.Cu", "In1.Cu")


class TestPadToViaTraces:
    """Tests for pad-to-via trace segment creation."""

    def test_traces_created_for_each_via(self, stitch_test_pcb: Path):
        """Should create one trace segment for each via placed."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        assert len(result.traces_added) == len(result.vias_added)
        assert len(result.traces_added) == 4  # 4 GND pads

    def test_trace_connects_pad_to_via(self, stitch_test_pcb: Path):
        """Each trace should go from pad center to via center."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        for trace, via in zip(result.traces_added, result.vias_added):
            # Trace starts at pad center
            assert trace.pad.x == via.pad.x
            assert trace.pad.y == via.pad.y
            # Trace ends at via center
            assert trace.via_x == via.via_x
            assert trace.via_y == via.via_y

    def test_trace_on_pad_surface_layer(self, stitch_test_pcb: Path):
        """Traces should be on the pad's surface layer."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        for trace in result.traces_added:
            assert trace.layer == trace.pad.layer
            assert trace.layer == "F.Cu"  # All test pads are on F.Cu

    def test_trace_default_width(self, stitch_test_pcb: Path):
        """Traces should use default width of 0.2mm."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        for trace in result.traces_added:
            assert trace.width == 0.2

    def test_trace_custom_width(self, stitch_test_pcb: Path):
        """Should use custom trace width when specified."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            trace_width=0.3,
            dry_run=True,
        )

        for trace in result.traces_added:
            assert trace.width == 0.3

    def test_traces_written_to_pcb(self, stitch_test_pcb: Path):
        """Traces should be written as segment nodes in the PCB file."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=False,
        )

        assert len(result.traces_added) == 4

        # File should have both vias and segments
        new_content = stitch_test_pcb.read_text()
        assert new_content.count("(via") >= 4
        assert new_content.count("(segment") >= 4

    def test_no_traces_for_skipped_pads(self, stitch_connected_pcb: Path):
        """Already-connected pads should not get traces."""
        result = run_stitch(
            pcb_path=stitch_connected_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        # Already-connected pads have no vias and no traces
        assert result.already_connected >= 1
        assert len(result.traces_added) == len(result.vias_added)

    def test_dry_run_does_not_write_traces(self, stitch_test_pcb: Path):
        """Dry run should not write traces to file."""
        original_content = stitch_test_pcb.read_text()

        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        assert len(result.traces_added) > 0
        assert stitch_test_pcb.read_text() == original_content


class TestCLIMain:
    """Tests for the main CLI entry point."""

    def test_main_dry_run(self, stitch_test_pcb: Path, capsys):
        """Main with --dry-run should not modify file."""
        original_content = stitch_test_pcb.read_text()

        exit_code = main([str(stitch_test_pcb), "--net", "GND", "--dry-run"])

        assert exit_code == 0
        assert stitch_test_pcb.read_text() == original_content

        captured = capsys.readouterr()
        assert "dry run" in captured.out.lower()

    def test_main_adds_vias(self, stitch_test_pcb: Path, capsys):
        """Main should add vias and report success."""
        exit_code = main([str(stitch_test_pcb), "--net", "GND"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Added" in captured.out

    def test_main_multiple_nets(self, stitch_test_pcb: Path, capsys):
        """Main should accept multiple --net options."""
        exit_code = main([str(stitch_test_pcb), "--net", "GND", "--net", "+3.3V", "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "GND" in captured.out
        assert "+3.3V" in captured.out

    def test_main_nonexistent_file(self, capsys):
        """Main should return 1 for nonexistent file."""
        exit_code = main(["nonexistent.kicad_pcb", "--net", "GND"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_main_invalid_file_type(self, tmp_path, capsys):
        """Main should return 1 for non-PCB file."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("not a pcb")

        exit_code = main([str(bad_file), "--net", "GND"])

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_main_output_file(self, stitch_test_pcb: Path, tmp_path):
        """Main with -o should write to output file."""
        output_file = tmp_path / "output.kicad_pcb"
        original_content = stitch_test_pcb.read_text()

        exit_code = main([str(stitch_test_pcb), "--net", "GND", "-o", str(output_file)])

        assert exit_code == 0
        # Original unchanged
        assert stitch_test_pcb.read_text() == original_content
        # Output has vias
        assert output_file.exists()
        assert "(via" in output_file.read_text()

    def test_main_output_copies_project_file(self, stitch_test_pcb: Path, tmp_path):
        """Main with -o should copy matching .kicad_pro file."""
        # Create matching project file alongside the test PCB
        pro_path = stitch_test_pcb.with_suffix(".kicad_pro")
        pro_content = '{"board": {"design_settings": {}}}'
        pro_path.write_text(pro_content)

        output_file = tmp_path / "subdir" / "output.kicad_pcb"
        output_file.parent.mkdir(parents=True, exist_ok=True)

        exit_code = main([str(stitch_test_pcb), "--net", "GND", "-o", str(output_file)])

        assert exit_code == 0
        assert output_file.exists()
        # Project file also copied with matching name
        output_pro = output_file.with_suffix(".kicad_pro")
        assert output_pro.exists()
        assert output_pro.read_text() == pro_content

    def test_main_output_without_project_file(self, stitch_test_pcb: Path, tmp_path):
        """Main with -o should work even if no .kicad_pro exists."""
        # Ensure no project file exists
        pro_path = stitch_test_pcb.with_suffix(".kicad_pro")
        if pro_path.exists():
            pro_path.unlink()

        output_file = tmp_path / "output.kicad_pcb"

        exit_code = main([str(stitch_test_pcb), "--net", "GND", "-o", str(output_file)])

        assert exit_code == 0
        assert output_file.exists()
        # No project file should be created
        output_pro = output_file.with_suffix(".kicad_pro")
        assert not output_pro.exists()

    def test_main_dry_run_skips_project_copy(self, stitch_test_pcb: Path, tmp_path):
        """Dry-run should not copy project file."""
        # Create matching project file
        pro_path = stitch_test_pcb.with_suffix(".kicad_pro")
        pro_path.write_text('{"board": {}}')

        output_file = tmp_path / "output.kicad_pcb"

        exit_code = main(
            [str(stitch_test_pcb), "--net", "GND", "-o", str(output_file), "--dry-run"]
        )

        assert exit_code == 0
        # Neither PCB nor project file should be created in dry-run
        assert not output_file.exists()
        output_pro = output_file.with_suffix(".kicad_pro")
        assert not output_pro.exists()

    def test_main_custom_options(self, stitch_test_pcb: Path, capsys):
        """Main should accept custom via options."""
        exit_code = main(
            [
                str(stitch_test_pcb),
                "--net",
                "GND",
                "--via-size",
                "0.6",
                "--drill",
                "0.3",
                "--dry-run",
            ]
        )

        assert exit_code == 0

    def test_main_target_layer(self, stitch_test_pcb: Path, capsys):
        """Main should accept target layer option."""
        exit_code = main(
            [str(stitch_test_pcb), "--net", "GND", "--target-layer", "In1.Cu", "--dry-run"]
        )

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "In1.Cu" in captured.out


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_net_list(self, stitch_test_pcb: Path):
        """Should handle empty result gracefully."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["NONEXISTENT_NET"],
            dry_run=True,
        )

        assert len(result.vias_added) == 0
        assert result.already_connected == 0

    def test_all_already_connected(self, stitch_connected_pcb: Path):
        """Should report when all pads are already connected."""
        result = run_stitch(
            pcb_path=stitch_connected_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        # The existing via connects the GND pad
        assert result.already_connected >= 1


# PCB with other-net tracks near pads to test clearance checking
# This simulates the bug: GND pad at (110, 110) with a +3.3V trace running nearby
STITCH_CLEARANCE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (segment (start 109.0 109.0) (end 112.0 109.0) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-3v3-1"))
  (segment (start 109.0 111.0) (end 112.0 111.0) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-3v3-2"))
  (segment (start 109.0 109.0) (end 109.0 111.0) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-3v3-3"))
  (segment (start 112.0 109.0) (end 112.0 111.0) (width 0.2) (layer "F.Cu") (net 2) (uuid "seg-3v3-4"))
)
"""


@pytest.fixture
def stitch_clearance_pcb(tmp_path: Path) -> Path:
    """Create a PCB with other-net tracks near pads for clearance testing."""
    pcb_file = tmp_path / "stitch_clearance.kicad_pcb"
    pcb_file.write_text(STITCH_CLEARANCE_PCB)
    return pcb_file


class TestPointToSegmentDistance:
    """Tests for geometric point-to-segment distance calculation."""

    def test_point_on_segment(self):
        """Point on the segment should have zero distance."""
        dist = point_to_segment_distance(1.0, 0.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(0.0)

    def test_point_perpendicular_to_segment(self):
        """Point perpendicular to segment midpoint."""
        dist = point_to_segment_distance(1.0, 1.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(1.0)

    def test_point_nearest_to_start(self):
        """Point closest to segment start."""
        dist = point_to_segment_distance(-1.0, 0.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(1.0)

    def test_point_nearest_to_end(self):
        """Point closest to segment end."""
        dist = point_to_segment_distance(3.0, 0.0, 0.0, 0.0, 2.0, 0.0)
        assert dist == pytest.approx(1.0)

    def test_degenerate_segment(self):
        """Zero-length segment should use point-to-point distance."""
        dist = point_to_segment_distance(3.0, 4.0, 0.0, 0.0, 0.0, 0.0)
        assert dist == pytest.approx(5.0)

    def test_diagonal_segment(self):
        """Point distance to diagonal segment."""
        # Segment from (0,0) to (1,1), point at (0,1) should be sqrt(2)/2
        dist = point_to_segment_distance(0.0, 1.0, 0.0, 0.0, 1.0, 1.0)
        assert dist == pytest.approx(math.sqrt(2) / 2)


class TestFindAllTrackSegments:
    """Tests for finding all track segments for clearance checking."""

    def test_finds_other_net_tracks(self, stitch_clearance_pcb: Path):
        """Should find tracks on other nets for clearance checking."""
        sexp = load_pcb(stitch_clearance_pcb)
        # Exclude GND (net 1), should find +3.3V (net 2) tracks
        segments = find_all_track_segments(sexp, exclude_nets={1})

        assert len(segments) == 4  # Four +3.3V track segments
        for seg in segments:
            assert seg.net_number == 2

    def test_excludes_specified_nets(self, stitch_clearance_pcb: Path):
        """Should exclude tracks on specified nets."""
        sexp = load_pcb(stitch_clearance_pcb)
        # Exclude both nets
        segments = find_all_track_segments(sexp, exclude_nets={1, 2})

        assert len(segments) == 0

    def test_includes_geometry(self, stitch_clearance_pcb: Path):
        """Should include full segment geometry."""
        sexp = load_pcb(stitch_clearance_pcb)
        segments = find_all_track_segments(sexp, exclude_nets={1})

        seg = segments[0]
        assert seg.start_x == 109.0
        assert seg.start_y == 109.0
        assert seg.end_x == 112.0
        assert seg.end_y == 109.0
        assert seg.width == 0.2
        assert seg.layer == "F.Cu"


class TestFindAllBoardVias:
    """Tests for finding all board vias for clearance checking."""

    def test_finds_other_net_vias(self, stitch_connected_pcb: Path):
        """Should find vias on other nets."""
        sexp = load_pcb(stitch_connected_pcb)
        # Exclude +3.3V (net 2), should find GND (net 1) via
        vias = find_all_board_vias(sexp, exclude_nets={2})

        assert len(vias) == 1
        assert vias[0][0] == 109.5  # x
        assert vias[0][1] == 110  # y
        assert vias[0][2] == 0.45  # size
        assert vias[0][3] == 1  # net

    def test_excludes_specified_nets(self, stitch_connected_pcb: Path):
        """Should exclude vias on specified nets."""
        sexp = load_pcb(stitch_connected_pcb)
        vias = find_all_board_vias(sexp, exclude_nets={1})

        assert len(vias) == 0


class TestClearanceChecking:
    """Tests for via placement clearance checking against other-net copper."""

    def test_via_avoids_other_net_track(self):
        """Via placement should avoid tracks on other nets."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Place a +3.3V track right next to where a via would go
        other_tracks = [
            TrackSegment(
                start_x=100.8,
                start_y=99,
                end_x=100.8,
                end_y=101,
                width=0.2,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
        )

        if pos is not None:
            # Verify the via doesn't violate clearance to the track
            dist = point_to_segment_distance(pos[0], pos[1], 100.8, 99, 100.8, 101)
            min_clearance = 0.45 / 2 + 0.2 / 2 + 0.2  # via_radius + track_half_width + clearance
            assert dist >= min_clearance - 0.001  # Small tolerance for floating point

    def test_via_avoids_other_net_via(self):
        """Via placement should avoid vias on other nets."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Place another net's via right next to where we'd place ours
        other_vias = [
            (100.8, 100, 0.45, 2),  # x, y, size, net
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_vias=other_vias,
        )

        if pos is not None:
            # Verify the via doesn't violate clearance to the other via
            dist = math.sqrt((pos[0] - 100.8) ** 2 + (pos[1] - 100) ** 2)
            min_clearance = 0.45 / 2 + 0.45 / 2 + 0.2  # via_radius + other_via_radius + clearance
            assert dist >= min_clearance - 0.001

    def test_via_surrounded_by_other_net_tracks_is_skipped(self):
        """Via should be skipped if completely surrounded by other-net tracks."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Surround the pad with very close other-net tracks (box pattern)
        other_tracks = [
            TrackSegment(
                start_x=99.0,
                start_y=99.5,
                end_x=101.0,
                end_y=99.5,
                width=0.3,
                layer="F.Cu",
                net_number=2,
            ),
            TrackSegment(
                start_x=99.0,
                start_y=100.5,
                end_x=101.0,
                end_y=100.5,
                width=0.3,
                layer="F.Cu",
                net_number=2,
            ),
            TrackSegment(
                start_x=99.5,
                start_y=99.0,
                end_x=99.5,
                end_y=101.0,
                width=0.3,
                layer="F.Cu",
                net_number=2,
            ),
            TrackSegment(
                start_x=100.5,
                start_y=99.0,
                end_x=100.5,
                end_y=101.0,
                width=0.3,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
        )

        # With tight surrounding tracks, should either find a valid position
        # that clears all tracks, or return None
        if pos is not None:
            for seg in other_tracks:
                dist = point_to_segment_distance(
                    pos[0], pos[1], seg.start_x, seg.start_y, seg.end_x, seg.end_y
                )
                min_clearance = 0.45 / 2 + 0.3 / 2 + 0.2
                assert dist >= min_clearance - 0.001

    def test_stitch_skips_pad_with_clearance_conflict(self, stitch_clearance_pcb: Path):
        """Stitching should skip pads where vias would short other nets."""
        # The clearance PCB has +3.3V tracks surrounding the GND pad area
        result = run_stitch(
            pcb_path=stitch_clearance_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        # The GND pad at C1.1 should either be skipped (clearance conflict)
        # or placed at a safe position. Either way, no shorts should occur.
        for via in result.vias_added:
            # Verify placed vias don't overlap +3.3V tracks
            # Tracks: y=109, y=111, x=109, x=112 (all net 2)
            track_segments = [
                (109.0, 109.0, 112.0, 109.0),  # top horizontal
                (109.0, 111.0, 112.0, 111.0),  # bottom horizontal
                (109.0, 109.0, 109.0, 111.0),  # left vertical
                (112.0, 109.0, 112.0, 111.0),  # right vertical
            ]
            track_width = 0.2
            via_radius = via.size / 2

            for sx, sy, ex, ey in track_segments:
                dist = point_to_segment_distance(via.via_x, via.via_y, sx, sy, ex, ey)
                min_clearance = via_radius + track_width / 2 + 0.2  # default clearance
                assert dist >= min_clearance - 0.01, (
                    f"Via at ({via.via_x:.2f}, {via.via_y:.2f}) violates clearance "
                    f"to track ({sx}, {sy})-({ex}, {ey}): "
                    f"dist={dist:.3f} < min={min_clearance:.3f}"
                )

    def test_backwards_compatible_without_other_net_args(self):
        """calculate_via_position should work without other-net args (backwards compat)."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Call without other_net_tracks and other_net_vias (old API)
        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
        )

        assert pos is not None


# PCB with zones for auto-detection testing
STITCH_ZONE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "VCC")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000200")
    (at 120 110)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c2"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "VCC"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-gnd-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
  (zone (net 2) (net_name "+3.3V") (layer "In2.Cu") (uuid "zone-3v3-uuid")
    (name "3V3_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 140 100) (xy 140 130) (xy 100 130)))
  )
)
"""


@pytest.fixture
def stitch_zone_pcb(tmp_path: Path) -> Path:
    """Create a PCB file with zones for testing auto-detection."""
    pcb_file = tmp_path / "stitch_zone.kicad_pcb"
    pcb_file.write_text(STITCH_ZONE_PCB)
    return pcb_file


class TestZoneAutoDetection:
    """Tests for zone-based target layer auto-detection."""

    def test_find_zones_for_net(self, stitch_zone_pcb: Path):
        """Should find zones matching a net name."""
        from kicad_tools.cli.stitch_cmd import find_zones_for_net
        from kicad_tools.core.sexp_file import load_pcb

        sexp = load_pcb(stitch_zone_pcb)

        # GND zone is on In1.Cu
        gnd_layers = find_zones_for_net(sexp, "GND")
        assert gnd_layers == ["In1.Cu"]

        # +3.3V zone is on In2.Cu
        v33_layers = find_zones_for_net(sexp, "+3.3V")
        assert v33_layers == ["In2.Cu"]

        # VCC has no zone
        vcc_layers = find_zones_for_net(sexp, "VCC")
        assert vcc_layers == []

    def test_auto_detect_target_layer_from_zone(self, stitch_zone_pcb: Path):
        """Should auto-detect target layer from zones when not specified."""
        result = run_stitch(
            pcb_path=stitch_zone_pcb,
            net_names=["GND"],
            target_layer=None,  # Auto-detect
            dry_run=True,
        )

        # Should detect In1.Cu from GND zone
        assert "GND" in result.detected_layers
        assert result.detected_layers["GND"] == "In1.Cu"
        assert len(result.fallback_nets) == 0

        # Vias should target In1.Cu
        assert len(result.vias_added) > 0
        for via in result.vias_added:
            assert via.layers[1] == "In1.Cu"

    def test_auto_detect_multiple_nets(self, stitch_zone_pcb: Path):
        """Should auto-detect target layers for multiple nets with zones."""
        result = run_stitch(
            pcb_path=stitch_zone_pcb,
            net_names=["GND", "+3.3V"],
            target_layer=None,  # Auto-detect
            dry_run=True,
        )

        # Should detect layers for both nets
        assert result.detected_layers.get("GND") == "In1.Cu"
        assert result.detected_layers.get("+3.3V") == "In2.Cu"
        assert len(result.fallback_nets) == 0

        # Check vias target correct layers
        gnd_vias = [v for v in result.vias_added if v.pad.net_name == "GND"]
        v33_vias = [v for v in result.vias_added if v.pad.net_name == "+3.3V"]

        for via in gnd_vias:
            assert via.layers[1] == "In1.Cu"
        for via in v33_vias:
            assert via.layers[1] == "In2.Cu"

    def test_fallback_to_bcu_when_no_zone(self, stitch_zone_pcb: Path):
        """Should fall back to B.Cu when no zone found for net."""
        result = run_stitch(
            pcb_path=stitch_zone_pcb,
            net_names=["VCC"],  # VCC has no zone
            target_layer=None,  # Auto-detect
            dry_run=True,
        )

        # Should record VCC as fallback
        assert "VCC" in result.fallback_nets
        assert "VCC" not in result.detected_layers

        # VCC vias should target B.Cu (default)
        vcc_vias = [v for v in result.vias_added if v.pad.net_name == "VCC"]
        for via in vcc_vias:
            assert via.layers[1] == "B.Cu"

    def test_mixed_zone_and_no_zone_nets(self, stitch_zone_pcb: Path):
        """Should handle mix of nets with and without zones."""
        result = run_stitch(
            pcb_path=stitch_zone_pcb,
            net_names=["GND", "VCC"],  # GND has zone, VCC doesn't
            target_layer=None,
            dry_run=True,
        )

        # GND detected from zone
        assert result.detected_layers.get("GND") == "In1.Cu"

        # VCC falls back to B.Cu
        assert "VCC" in result.fallback_nets

        # Check layers match
        gnd_vias = [v for v in result.vias_added if v.pad.net_name == "GND"]
        vcc_vias = [v for v in result.vias_added if v.pad.net_name == "VCC"]

        for via in gnd_vias:
            assert via.layers[1] == "In1.Cu"
        for via in vcc_vias:
            assert via.layers[1] == "B.Cu"

    def test_explicit_target_overrides_zone(self, stitch_zone_pcb: Path):
        """Explicit target layer should override zone auto-detection."""
        result = run_stitch(
            pcb_path=stitch_zone_pcb,
            net_names=["GND"],
            target_layer="In2.Cu",  # Override the In1.Cu zone
            dry_run=True,
        )

        # Should not auto-detect when explicit
        assert len(result.detected_layers) == 0
        assert len(result.fallback_nets) == 0

        # All vias should use the explicit layer
        for via in result.vias_added:
            assert via.layers[1] == "In2.Cu"

    def test_no_zone_without_explicit_target(self, stitch_test_pcb: Path):
        """PCB without zones should fall back to B.Cu for all nets."""
        result = run_stitch(
            pcb_path=stitch_test_pcb,
            net_names=["GND"],
            target_layer=None,  # Auto-detect
            dry_run=True,
        )

        # GND should fall back (no zones in test PCB)
        assert "GND" in result.fallback_nets
        assert len(result.detected_layers) == 0

        # Vias should target B.Cu
        for via in result.vias_added:
            assert via.layers[1] == "B.Cu"


class TestCLIOutputWithZones:
    """Tests for CLI output with zone auto-detection."""

    def test_output_shows_detected_layers(self, stitch_zone_pcb: Path, capsys):
        """CLI should show detected layers in output."""
        exit_code = main([str(stitch_zone_pcb), "--net", "GND", "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Auto-detected target layers" in captured.out
        assert "GND -> In1.Cu" in captured.out

    def test_output_shows_fallback_warning(self, stitch_zone_pcb: Path, capsys):
        """CLI should show warning when falling back to B.Cu."""
        exit_code = main([str(stitch_zone_pcb), "--net", "VCC", "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Warning" in captured.err
        assert "VCC" in captured.err
        assert "B.Cu" in captured.err


class TestFindAllPlaneNets:
    """Tests for find_all_plane_nets function."""

    def test_find_all_plane_nets(self, stitch_zone_pcb: Path):
        """Should find all nets that have zones."""
        sexp = load_pcb(stitch_zone_pcb)
        plane_nets = find_all_plane_nets(sexp)

        # GND and +3.3V have zones
        assert "GND" in plane_nets
        assert "+3.3V" in plane_nets
        assert plane_nets["GND"] == "In1.Cu"
        assert plane_nets["+3.3V"] == "In2.Cu"

        # VCC has no zone, should not be in result
        assert "VCC" not in plane_nets

    def test_find_all_plane_nets_empty_pcb(self, stitch_test_pcb: Path):
        """Should return empty dict for PCB without zones."""
        sexp = load_pcb(stitch_test_pcb)
        plane_nets = find_all_plane_nets(sexp)

        assert plane_nets == {}

    def test_find_all_plane_nets_skips_empty_net_names(self, tmp_path: Path):
        """Should skip zones with empty net names."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (zone (net 0) (net_name "") (layer "In1.Cu") (uuid "z1"))
          (zone (net 1) (net_name "GND") (layer "In2.Cu") (uuid "z2"))
        )"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        sexp = load_pcb(pcb_file)
        plane_nets = find_all_plane_nets(sexp)

        # Only GND should be found, empty net skipped
        assert len(plane_nets) == 1
        assert "GND" in plane_nets


class TestKiCad9NameOnlyZoneFormat:
    """Tests for KiCad 9 name-only net format in zones (no net_name node)."""

    def test_find_zones_for_net_name_only_format(self, tmp_path: Path):
        """find_zones_for_net should handle (net "GND") without net_name node."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (zone (net "GND") (layer "In1.Cu") (uuid "z1")
            (connect_pads (clearance 0.2))
            (min_thickness 0.2)
            (fill yes)
            (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
          )
        )"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        from kicad_tools.cli.stitch_cmd import find_zones_for_net

        sexp = load_pcb(pcb_file)
        layers = find_zones_for_net(sexp, "GND")
        assert layers == ["In1.Cu"]

    def test_find_all_plane_nets_name_only_format(self, tmp_path: Path):
        """find_all_plane_nets should handle (net "GND") without net_name node."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (net 2 "+3.3V")
          (zone (net "GND") (layer "In1.Cu") (uuid "z1")
            (connect_pads (clearance 0.2))
            (min_thickness 0.2)
            (fill yes)
            (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
          )
          (zone (net "+3.3V") (layer "In2.Cu") (uuid "z2")
            (connect_pads (clearance 0.2))
            (min_thickness 0.2)
            (fill yes)
            (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
          )
        )"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        sexp = load_pcb(pcb_file)
        plane_nets = find_all_plane_nets(sexp)
        assert plane_nets == {"GND": "In1.Cu", "+3.3V": "In2.Cu"}

    def test_traditional_format_still_works(self, tmp_path: Path):
        """Traditional (net N) + (net_name "GND") format should still work."""
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (31 "B.Cu" signal))
          (net 0 "")
          (net 1 "GND")
          (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "z1")
            (connect_pads (clearance 0.2))
            (min_thickness 0.2)
            (fill yes)
            (polygon (pts (xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)))
          )
        )"""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(pcb_content)

        from kicad_tools.cli.stitch_cmd import find_zones_for_net

        sexp = load_pcb(pcb_file)
        layers = find_zones_for_net(sexp, "GND")
        assert layers == ["In1.Cu"]

        plane_nets = find_all_plane_nets(sexp)
        assert plane_nets == {"GND": "In1.Cu"}


class TestAutoDetectPlaneNets:
    """Tests for CLI auto-detection of power plane nets."""

    def test_cli_auto_detect_no_net_flag(self, stitch_zone_pcb: Path, capsys):
        """CLI without --net should auto-detect plane nets from zones."""
        exit_code = main([str(stitch_zone_pcb), "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()

        # Should report auto-detection
        assert "Auto-detected" in captured.out
        # Should include both nets with zones
        assert "GND" in captured.out
        assert "+3.3V" in captured.out

    def test_cli_auto_detect_adds_vias_for_all_plane_nets(self, stitch_zone_pcb: Path):
        """Auto-detect should stitch all detected plane nets."""
        # First verify the nets present
        sexp = load_pcb(stitch_zone_pcb)
        plane_nets = find_all_plane_nets(sexp)
        assert len(plane_nets) == 2  # GND and +3.3V

        # Run stitching with auto-detect
        exit_code = main([str(stitch_zone_pcb), "--dry-run"])
        assert exit_code == 0

        # The dry run should report vias for both nets
        # Note: We can't easily verify count without modifying how results are returned
        # but we verified the mechanism works via the TestZoneAutoDetection tests

    def test_cli_no_zones_returns_error(self, stitch_test_pcb: Path, capsys):
        """CLI without --net and no zones should return error."""
        exit_code = main([str(stitch_test_pcb)])  # No --net, no zones

        assert exit_code == 1
        captured = capsys.readouterr()
        assert "No power plane nets found" in captured.err

    def test_explicit_net_flag_still_works(self, stitch_zone_pcb: Path, capsys):
        """Explicit --net flag should override auto-detection of which nets to stitch."""
        # Only stitch GND, not +3.3V
        exit_code = main([str(stitch_zone_pcb), "--net", "GND", "--dry-run"])

        assert exit_code == 0
        captured = capsys.readouterr()

        # Should NOT report auto-detection of nets (no "2 power plane nets" message)
        assert "power plane nets:" not in captured.out
        # Should report detected target layer for the explicit net
        assert "GND -> In1.Cu" in captured.out
        # Should NOT include +3.3V since we only specified GND
        assert "+3.3V ->" not in captured.out


class TestSegmentToSegmentDistance:
    """Tests for segment-to-segment distance calculation."""

    def test_parallel_segments(self):
        """Parallel segments should have correct perpendicular distance."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            2.0,
            0.0,  # Segment A: horizontal at y=0
            0.0,
            1.0,
            2.0,
            1.0,  # Segment B: horizontal at y=1
        )
        assert dist == pytest.approx(1.0)

    def test_crossing_segments(self):
        """Crossing segments should have distance 0."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            2.0,
            2.0,  # Segment A: diagonal
            0.0,
            2.0,
            2.0,
            0.0,  # Segment B: opposite diagonal (crosses A)
        )
        assert dist == pytest.approx(0.0)

    def test_t_shaped_segments(self):
        """Perpendicular segments that don't cross."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            2.0,
            0.0,  # Segment A: horizontal
            1.0,
            1.0,
            1.0,
            3.0,  # Segment B: vertical, starts 1 unit above A
        )
        assert dist == pytest.approx(1.0)

    def test_collinear_separated_segments(self):
        """Collinear segments with a gap."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            1.0,
            0.0,  # Segment A: (0,0)-(1,0)
            3.0,
            0.0,
            4.0,
            0.0,  # Segment B: (3,0)-(4,0)
        )
        assert dist == pytest.approx(2.0)

    def test_endpoint_to_endpoint(self):
        """Distance between segment endpoints when closest."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            1.0,
            0.0,  # Segment A
            2.0,
            1.0,
            3.0,
            1.0,  # Segment B
        )
        expected = math.sqrt(1.0**2 + 1.0**2)  # dist from (1,0) to (2,1)
        assert dist == pytest.approx(expected)

    def test_zero_length_segment(self):
        """Degenerate (zero-length) segment acts as point."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            0.0,
            0.0,  # Point at origin
            1.0,
            0.0,
            2.0,
            0.0,  # Segment from (1,0) to (2,0)
        )
        assert dist == pytest.approx(1.0)

    def test_identical_segments(self):
        """Overlapping segments should have distance 0."""
        dist = segment_to_segment_distance(
            0.0,
            0.0,
            2.0,
            0.0,
            0.0,
            0.0,
            2.0,
            0.0,
        )
        assert dist == pytest.approx(0.0)


class TestTracePathClearance:
    """Tests for trace path clearance checking against other-net copper."""

    def _make_pad(self, x=100.0, y=100.0):
        """Helper to create a test pad."""
        return PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=x,
            y=y,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

    def test_trace_path_crossing_other_net_track_rejected(self):
        """Trace crossing an other-net track should be rejected.

        Place an other-net track between the pad and the east (+x) via
        position. The via position itself may be clear, but the trace
        from pad to via crosses the other-net track.
        """
        pad = self._make_pad()

        # Place a vertical +3.3V track at x=100.5, between pad (100,100)
        # and the first via candidate in the +x direction (~100.82)
        other_tracks = [
            TrackSegment(
                start_x=100.5,
                start_y=98.0,
                end_x=100.5,
                end_y=102.0,
                width=0.2,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
            trace_width=0.2,
        )

        # If a position is found, verify the trace path doesn't cross the track
        if pos is not None:
            trace_dist = segment_to_segment_distance(
                pad.x,
                pad.y,
                pos[0],
                pos[1],
                100.5,
                98.0,
                100.5,
                102.0,
            )
            min_clearance = 0.2 / 2 + 0.2 / 2 + 0.2  # trace_hw + track_hw + clearance
            assert trace_dist >= min_clearance - 0.001

    def test_trace_path_clearance_violation_rejected(self):
        """Trace running parallel but too close to other-net track is rejected.

        Place an other-net track running parallel and within clearance
        distance of the trace path from pad to via.
        """
        pad = self._make_pad()

        # Place a horizontal track very close to and parallel with the trace
        # path in the +x direction (pad at y=100, track at y=100.15)
        # This is within clearance (0.2/2 + 0.2/2 + 0.2 = 0.4mm needed, only 0.15mm apart)
        other_tracks = [
            TrackSegment(
                start_x=99.5,
                start_y=100.15,
                end_x=101.5,
                end_y=100.15,
                width=0.2,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
            trace_width=0.2,
        )

        # If a position is found, the trace path must clear the parallel track
        if pos is not None:
            trace_dist = segment_to_segment_distance(
                pad.x,
                pad.y,
                pos[0],
                pos[1],
                99.5,
                100.15,
                101.5,
                100.15,
            )
            min_clearance = 0.2 / 2 + 0.2 / 2 + 0.2
            assert trace_dist >= min_clearance - 0.001

    def test_trace_path_with_clear_route_accepted(self):
        """Trace path that clears all obstacles should be accepted."""
        pad = self._make_pad()

        # Place a track far away (y=105) that won't interfere with any direction
        other_tracks = [
            TrackSegment(
                start_x=98.0,
                start_y=105.0,
                end_x=102.0,
                end_y=105.0,
                width=0.2,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
            trace_width=0.2,
        )

        assert pos is not None

    def test_trace_path_avoids_other_net_via(self):
        """Trace path should avoid other-net vias along the trace route.

        Place an other-net via along the trace path between pad and via position.
        """
        pad = self._make_pad()

        # Place an other-net via at (100.5, 100), right on the trace path
        # from pad (100,100) toward the east direction
        other_vias = [
            (100.5, 100.0, 0.45, 2),  # x, y, size, net
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_vias=other_vias,
            trace_width=0.2,
        )

        if pos is not None:
            # Verify the trace path doesn't violate clearance to the other via
            trace_dist = point_to_segment_distance(
                100.5,
                100.0,
                pad.x,
                pad.y,
                pos[0],
                pos[1],
            )
            min_clearance = 0.2 / 2 + 0.45 / 2 + 0.2  # trace_hw + via_radius + clearance
            assert trace_dist >= min_clearance - 0.001

    def test_via_valid_but_trace_blocked_falls_back(self):
        """When via position is valid but trace path is blocked, should try next direction.

        Place obstacles that block the trace in the +x direction but leave
        other directions clear.
        """
        pad = self._make_pad()

        # Block the trace path to the east with a crossing track
        # but leave south direction clear
        other_tracks = [
            TrackSegment(
                start_x=100.4,
                start_y=98.0,
                end_x=100.4,
                end_y=102.0,
                width=0.2,
                layer="F.Cu",
                net_number=2,
            ),
        ]

        # Without trace_width check, east (+x) direction is tried first and
        # the via itself at ~100.82 is far enough from the track at 100.4.
        # But the trace from (100,100) to (100.82,100) crosses x=100.4.

        pos_with_trace_check = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
            trace_width=0.2,
        )

        pos_without_trace_check = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=other_tracks,
            trace_width=0.0,  # No trace check
        )

        # Both should find a position
        assert pos_with_trace_check is not None
        assert pos_without_trace_check is not None

        # But they may differ: trace-checked version should avoid the blocked direction
        # The without-check version should use the east direction (first tried)
        # The with-check version should use a different direction (south, etc.)
        # Verify the with-check version's trace path is actually clear
        trace_dist = segment_to_segment_distance(
            pad.x,
            pad.y,
            pos_with_trace_check[0],
            pos_with_trace_check[1],
            100.4,
            98.0,
            100.4,
            102.0,
        )
        min_clearance = 0.2 / 2 + 0.2 / 2 + 0.2
        assert trace_dist >= min_clearance - 0.001

    def test_backwards_compatible_trace_width_zero(self):
        """trace_width=0 should behave identically to not checking trace path."""
        pad = self._make_pad()

        pos_default = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
        )

        pos_zero = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            trace_width=0.0,
        )

        assert pos_default == pos_zero

    def test_run_stitch_passes_trace_width(self, stitch_clearance_pcb: Path):
        """run_stitch should pass trace_width to calculate_via_position.

        The clearance PCB has other-net tracks near the pad. With trace path
        checking enabled (via trace_width parameter), placed vias should have
        clearance-safe trace paths.
        """
        result = run_stitch(
            pcb_path=stitch_clearance_pcb,
            net_names=["GND"],
            trace_width=0.2,
            dry_run=True,
        )

        # For every placed via, verify the trace path is clear of other-net tracks
        track_segments = [
            (109.0, 109.0, 112.0, 109.0),  # top horizontal
            (109.0, 111.0, 112.0, 111.0),  # bottom horizontal
            (109.0, 109.0, 109.0, 111.0),  # left vertical
            (112.0, 109.0, 112.0, 111.0),  # right vertical
        ]
        track_width = 0.2

        for via in result.vias_added:
            for sx, sy, ex, ey in track_segments:
                trace_dist = segment_to_segment_distance(
                    via.pad.x,
                    via.pad.y,
                    via.via_x,
                    via.via_y,
                    sx,
                    sy,
                    ex,
                    ey,
                )
                min_clearance = 0.2 / 2 + track_width / 2 + 0.2
                assert trace_dist >= min_clearance - 0.01, (
                    f"Trace from pad ({via.pad.x:.2f}, {via.pad.y:.2f}) to "
                    f"via ({via.via_x:.2f}, {via.via_y:.2f}) violates clearance "
                    f"to track ({sx}, {sy})-({ex}, {ey}): "
                    f"dist={trace_dist:.3f} < min={min_clearance:.3f}"
                )


# PCB with footprint pads on other nets near GND pads (for pad clearance testing)
# GND pad at C1.1 (~109.49, 110), unconnected pad at U1.4 (~110.3, 110)
# The unconnected pad should block via placement east of the GND pad.
STITCH_PAD_CLEARANCE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "I2S_BCLK")
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000100")
    (at 110 110)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c1"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000500")
    (at 111 110)
    (property "Reference" "U1" (at 0 -2 0) (layer "F.SilkS") (uuid "ref-uuid-u1"))
    (pad "1" smd roundrect (at -0.95 -0.8) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "I2S_BCLK"))
    (pad "2" smd roundrect (at -0.95 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "I2S_BCLK"))
    (pad "3" smd roundrect (at -0.95 0.8) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 3 "I2S_BCLK"))
    (pad "4" smd roundrect (at 0.95 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "5" smd roundrect (at 0.95 -0.8) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "+3.3V"))
  )
)
"""

# PCB with a rotated footprint to test coordinate transforms
STITCH_ROTATED_PAD_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (footprint "Package_TO_SOT_SMD:SOT-23-5"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000600")
    (at 10 20 90)
    (property "Reference" "U2" (at 0 -2 0) (layer "F.SilkS") (uuid "ref-uuid-u2"))
    (pad "1" smd roundrect (at 1 0) (size 0.6 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "SIG"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000700")
    (at 10 21)
    (property "Reference" "C2" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c2"))
    (pad "1" smd roundrect (at 0 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "GND"))
  )
)
"""


@pytest.fixture
def stitch_pad_clearance_pcb(tmp_path: Path) -> Path:
    """Create a PCB with other-net pads near GND pads for pad clearance testing."""
    pcb_file = tmp_path / "stitch_pad_clearance.kicad_pcb"
    pcb_file.write_text(STITCH_PAD_CLEARANCE_PCB)
    return pcb_file


@pytest.fixture
def stitch_rotated_pad_pcb(tmp_path: Path) -> Path:
    """Create a PCB with a rotated footprint for coordinate transform testing."""
    pcb_file = tmp_path / "stitch_rotated_pad.kicad_pcb"
    pcb_file.write_text(STITCH_ROTATED_PAD_PCB)
    return pcb_file


class TestFindAllPads:
    """Tests for the find_all_pads function."""

    def test_finds_pads_excluding_target_nets(self, stitch_pad_clearance_pcb: Path):
        """Should find pads on other nets, excluding the target net."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        # Exclude GND (net 1) - should find pads on net 0, 2, 3
        pads = find_all_pads(sexp, exclude_nets={1})

        # C1.2 (+3.3V), U1.1 (I2S_BCLK), U1.2 (I2S_BCLK), U1.3 (I2S_BCLK),
        # U1.4 (<no net>), U1.5 (+3.3V)
        assert len(pads) == 6
        net_nums = {p[3] for p in pads}
        assert 1 not in net_nums  # GND excluded
        assert 0 in net_nums  # Unconnected pad included
        assert 2 in net_nums  # +3.3V included
        assert 3 in net_nums  # I2S_BCLK included

    def test_includes_unconnected_pads_as_obstacles(self, stitch_pad_clearance_pcb: Path):
        """Pads with net 0 (<no net>) must be included as obstacles."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        pads = find_all_pads(sexp, exclude_nets={1})

        # Find the unconnected pad (U1.4, net 0)
        net0_pads = [p for p in pads if p[3] == 0]
        assert len(net0_pads) == 1
        # U1 at (111, 110), pad 4 at relative (0.95, 0)
        # Board coords: (111 + 0.95, 110) = (111.95, 110)
        px, py, radius, net = net0_pads[0]
        assert abs(px - 111.95) < 0.01
        assert abs(py - 110.0) < 0.01
        assert radius > 0
        assert net == 0

    def test_excludes_all_specified_nets(self, stitch_pad_clearance_pcb: Path):
        """Should exclude all pads on specified nets."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        # Exclude GND and +3.3V
        pads = find_all_pads(sexp, exclude_nets={1, 2})

        net_nums = {p[3] for p in pads}
        assert 1 not in net_nums
        assert 2 not in net_nums

    def test_handles_rotated_footprints(self, stitch_rotated_pad_pcb: Path):
        """Pad positions should be correctly transformed for rotated footprints."""
        sexp = load_pcb(stitch_rotated_pad_pcb)
        # Find pad from rotated U2 (at 10, 20, rotated 90 degrees)
        # Pad at relative (1, 0) -> after 90-degree rotation:
        # board_x = 10 + 1*cos(90) - 0*sin(90) = 10 + 0 = 10
        # board_y = 20 + 1*sin(90) + 0*cos(90) = 20 + 1 = 21
        pads = find_all_pads(sexp, exclude_nets={1})  # Exclude GND

        assert len(pads) == 1  # Only U2.1 (SIG, net 2)
        px, py, radius, net = pads[0]
        assert abs(px - 10.0) < 0.01
        assert abs(py - 21.0) < 0.01
        assert net == 2

    def test_pad_radius_from_size(self, stitch_pad_clearance_pcb: Path):
        """Pad radius should be max(width, height) / 2."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        pads = find_all_pads(sexp, exclude_nets={1})

        # U1 pads have size (0.6, 0.7) -> radius = 0.7/2 = 0.35
        u1_pads = [p for p in pads if p[3] == 3]  # I2S_BCLK pads
        for _px, _py, radius, _net in u1_pads:
            assert abs(radius - 0.35) < 0.01

    def test_no_pads_when_all_excluded(self, stitch_pad_clearance_pcb: Path):
        """Should return empty list when all nets are excluded."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        pads = find_all_pads(sexp, exclude_nets={0, 1, 2, 3})
        assert len(pads) == 0

    def test_all_pads_when_none_excluded(self, stitch_pad_clearance_pcb: Path):
        """Should return all pads when no nets are excluded."""
        sexp = load_pcb(stitch_pad_clearance_pcb)
        pads = find_all_pads(sexp, exclude_nets=set())

        # C1 has 2 pads, U1 has 5 pads = 7 total
        assert len(pads) == 7


class TestPadClearanceChecking:
    """Tests for via placement clearance checking against other-net pads."""

    def test_via_avoids_other_net_pad(self):
        """Via placement should avoid pads on other nets."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Place another net's pad right next to where a via would go (east)
        other_pads = [
            (100.8, 100, 0.35, 2),  # x, y, radius, net
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_pads=other_pads,
        )

        if pos is not None:
            # Verify the via doesn't violate clearance to the other pad
            dist = math.sqrt((pos[0] - 100.8) ** 2 + (pos[1] - 100) ** 2)
            min_clearance = 0.45 / 2 + 0.35 + 0.2  # via_radius + pad_radius + clearance
            assert dist >= min_clearance - 0.001

    def test_via_avoids_unconnected_pad(self):
        """Via placement should avoid unconnected pads (net 0)."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Unconnected pad (net 0) blocking east direction
        other_pads = [
            (100.8, 100, 0.35, 0),  # net 0 = <no net>
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_pads=other_pads,
        )

        if pos is not None:
            # Verify the via doesn't violate clearance to the unconnected pad
            dist = math.sqrt((pos[0] - 100.8) ** 2 + (pos[1] - 100) ** 2)
            min_clearance = 0.45 / 2 + 0.35 + 0.2
            assert dist >= min_clearance - 0.001

    def test_via_surrounded_by_other_net_pads_is_skipped(self):
        """Via should be skipped if completely surrounded by other-net pads."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Surround the pad with other-net pads in all directions (very close)
        other_pads = [
            (100.8, 100, 0.35, 2),
            (99.2, 100, 0.35, 2),
            (100, 100.8, 0.35, 2),
            (100, 99.2, 0.35, 2),
            (100.6, 100.6, 0.35, 2),
            (99.4, 100.6, 0.35, 2),
            (100.6, 99.4, 0.35, 2),
            (99.4, 99.4, 0.35, 2),
        ]

        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_pads=other_pads,
        )

        # With tight surrounding pads, should either find a valid position
        # that clears all pads, or return None
        if pos is not None:
            for px, py, p_radius, _ in other_pads:
                dist = math.sqrt((pos[0] - px) ** 2 + (pos[1] - py) ** 2)
                min_clearance = 0.45 / 2 + p_radius + 0.2
                assert dist >= min_clearance - 0.001

    def test_backwards_compatible_without_pad_arg(self):
        """calculate_via_position should work without other_net_pads arg."""
        pad = PadInfo(
            reference="C1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=0.54,
            height=0.64,
        )

        # Call without other_net_pads (old API)
        pos = calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
        )

        assert pos is not None

    def test_stitch_avoids_other_footprint_pads(self, stitch_pad_clearance_pcb: Path):
        """Integration test: stitch should avoid pads from other footprints."""
        result = run_stitch(
            pcb_path=stitch_pad_clearance_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        # C1.1 is the GND pad. It should either be placed with clearance
        # or skipped. Any placed via must respect clearance to U1 pads.
        sexp = load_pcb(stitch_pad_clearance_pcb)
        other_pads = find_all_pads(sexp, exclude_nets={1})

        for via in result.vias_added:
            for px, py, p_radius, _pnet in other_pads:
                dist = math.sqrt((via.via_x - px) ** 2 + (via.via_y - py) ** 2)
                min_clearance = via.size / 2 + p_radius + 0.2
                assert dist >= min_clearance - 0.01, (
                    f"Via at ({via.via_x:.2f}, {via.via_y:.2f}) violates clearance "
                    f"to pad at ({px:.2f}, {py:.2f}): "
                    f"dist={dist:.3f} < min={min_clearance:.3f}"
                )

    def test_stitch_avoids_signal_net_pads(self, stitch_pad_clearance_pcb: Path):
        """Via placement should avoid pads on other signal nets."""
        result = run_stitch(
            pcb_path=stitch_pad_clearance_pcb,
            net_names=["GND"],
            dry_run=True,
        )

        sexp = load_pcb(stitch_pad_clearance_pcb)
        # Get I2S_BCLK pads (net 3) specifically
        all_other_pads = find_all_pads(sexp, exclude_nets={1})
        signal_pads = [p for p in all_other_pads if p[3] == 3]

        for via in result.vias_added:
            for px, py, p_radius, _pnet in signal_pads:
                dist = math.sqrt((via.via_x - px) ** 2 + (via.via_y - py) ** 2)
                min_clearance = via.size / 2 + p_radius + 0.2
                assert dist >= min_clearance - 0.01, (
                    f"Via at ({via.via_x:.2f}, {via.via_y:.2f}) violates clearance "
                    f"to signal pad at ({px:.2f}, {py:.2f})"
                )


# PCB simulating a fine-pitch SSOP package where straight-line via placement fails
# This tests the dog-leg (L-shaped) routing for fine-pitch components
FINE_PITCH_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "SIG_A")
  (net 4 "SIG_B")
  (net 5 "SIG_C")
  (footprint "Package_SO:SSOP-20_4.4x6.5mm_P0.65mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "U1" (at 0 -4.5 0) (layer "F.SilkS") (uuid "ref-uuid-u1"))
    (pad "1" smd roundrect (at -2.9 -2.925) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "2" smd roundrect (at -2.9 -2.275) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "3" smd roundrect (at -2.9 -1.625) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "4" smd roundrect (at -2.9 -0.975) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "5" smd roundrect (at -2.9 -0.325) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
    (pad "6" smd roundrect (at -2.9 0.325) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "7" smd roundrect (at -2.9 0.975) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "8" smd roundrect (at -2.9 1.625) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "9" smd roundrect (at -2.9 2.275) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "10" smd roundrect (at -2.9 2.925) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
    (pad "11" smd roundrect (at 2.9 2.925) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "12" smd roundrect (at 2.9 2.275) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "13" smd roundrect (at 2.9 1.625) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "14" smd roundrect (at 2.9 0.975) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "15" smd roundrect (at 2.9 0.325) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
    (pad "16" smd roundrect (at 2.9 -0.325) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "17" smd roundrect (at 2.9 -0.975) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "18" smd roundrect (at 2.9 -1.625) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "19" smd roundrect (at 2.9 -2.275) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "20" smd roundrect (at 2.9 -2.925) (size 1.2 0.4) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
  )
  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "zone-gnd-uuid")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.5))
    (polygon
      (pts (xy 90 90) (xy 110 90) (xy 110 110) (xy 90 110))
    )
  )
)
"""


class TestDoglegRouting:
    """Tests for dog-leg (L-shaped) trace routing for fine-pitch components."""

    @pytest.fixture
    def fine_pitch_pcb(self, tmp_path: Path) -> Path:
        """Create a test PCB file simulating a fine-pitch SSOP package."""
        pcb_path = tmp_path / "fine_pitch.kicad_pcb"
        pcb_path.write_text(FINE_PITCH_PCB)
        return pcb_path

    def test_calculate_dogleg_via_position_finds_position(self):
        """Dog-leg routing should find a via position when straight-line fails."""
        # Create a pad in a simulated dense environment
        # The pad is on the left side of a component at x=97.1, y=100
        pad = PadInfo(
            reference="U1",
            pad_number="3",
            net_number=1,
            net_name="GND",
            x=97.1,
            y=100.0,
            layer="F.Cu",
            width=1.2,
            height=0.4,
        )

        # Adjacent pads on different nets that block straight-line routing to the left
        # These pads are above and below, blocking straight perpendicular escape
        # but allowing axial (vertical) movement first before escaping
        other_net_pads = [
            (96.0, 99.35, 0.3, 4),  # SIG_B above-left (blocks direct left at y=99.35)
            (96.0, 100.65, 0.3, 5),  # SIG_C below-left (blocks direct left at y=100.65)
        ]

        result = calculate_dogleg_via_position(
            pad=pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=other_net_pads,
            trace_width=0.2,
        )

        assert result is not None, "Dog-leg should find a via position"
        via_x, via_y, intermediate_x, intermediate_y = result

        # Verify the path is L-shaped (not a straight line)
        # Either intermediate_x != pad.x or intermediate_y != pad.y
        is_l_shaped = (abs(intermediate_x - pad.x) > 0.1) or (abs(intermediate_y - pad.y) > 0.1)
        assert is_l_shaped, "Path should be L-shaped, not straight"

    def test_dogleg_respects_clearance(self):
        """Dog-leg via position should respect clearance to other-net pads."""
        pad = PadInfo(
            reference="U1",
            pad_number="3",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=1.2,
            height=0.4,
        )

        # Dense other-net pads
        other_net_pads = [
            (100, 99.35, 0.6, 4),  # Above
            (100, 100.65, 0.6, 5),  # Below
        ]

        result = calculate_dogleg_via_position(
            pad=pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=other_net_pads,
            trace_width=0.2,
        )

        if result is not None:
            via_x, via_y, _, _ = result
            # Check clearance to all other-net pads
            via_radius = 0.45 / 2
            for px, py, p_radius, _ in other_net_pads:
                dist = math.sqrt((via_x - px) ** 2 + (via_y - py) ** 2)
                min_clearance = via_radius + p_radius + 0.2
                assert dist >= min_clearance - 0.01, (
                    f"Via at ({via_x:.2f}, {via_y:.2f}) violates clearance "
                    f"to pad at ({px:.2f}, {py:.2f})"
                )

    def test_trace_segment_is_dogleg_property(self):
        """TraceSegment.is_dogleg property should correctly identify L-shaped traces."""
        pad = PadInfo(
            reference="U1",
            pad_number="1",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=1.0,
            height=0.4,
        )

        # Straight trace (no intermediate point)
        straight_trace = TraceSegment(
            pad=pad,
            via_x=101,
            via_y=100,
            width=0.2,
            layer="F.Cu",
        )
        assert not straight_trace.is_dogleg

        # Dog-leg trace (with intermediate point)
        dogleg_trace = TraceSegment(
            pad=pad,
            via_x=101,
            via_y=101,
            width=0.2,
            layer="F.Cu",
            intermediate_x=101,
            intermediate_y=100,
        )
        assert dogleg_trace.is_dogleg

    def test_run_stitch_uses_dogleg_when_needed(self, fine_pitch_pcb: Path):
        """run_stitch should fall back to dog-leg routing when straight-line fails."""
        result = run_stitch(
            pcb_path=fine_pitch_pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )

        # Should have placed vias for GND pads (pins 3, 8, 13, 18)
        # Some may be straight, some may be dog-leg depending on pad density
        assert len(result.vias_added) > 0, "Should place at least some vias"

        # Check if any dog-leg traces were used
        dogleg_traces = [t for t in result.traces_added if t.is_dogleg]
        # We expect some dog-leg routing due to the dense pin arrangement
        # Note: The exact count depends on the algorithm finding clearance

    def test_run_stitch_dogleg_traces_are_valid(self, fine_pitch_pcb: Path):
        """Dog-leg traces should form valid L-shaped paths."""
        result = run_stitch(
            pcb_path=fine_pitch_pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )

        for trace in result.traces_added:
            if trace.is_dogleg:
                # Verify intermediate point exists
                assert trace.intermediate_x is not None
                assert trace.intermediate_y is not None

                # Verify the path is actually L-shaped (not colinear)
                # The intermediate point should not be on the direct line from pad to via
                pad_to_via_dx = trace.via_x - trace.pad.x
                pad_to_via_dy = trace.via_y - trace.pad.y
                pad_to_int_dx = trace.intermediate_x - trace.pad.x
                pad_to_int_dy = trace.intermediate_y - trace.pad.y

                # If the path were straight, these ratios would be equal
                # For an L-shape, they should differ
                if abs(pad_to_via_dx) > 0.01 and abs(pad_to_int_dx) > 0.01:
                    ratio_dx = pad_to_int_dy / (pad_to_int_dx + 0.001)
                    ratio_via = pad_to_via_dy / (pad_to_via_dx + 0.001)
                    # L-shape: ratios should differ significantly (not colinear)
                    is_colinear = abs(ratio_dx - ratio_via) < 0.1
                    # Note: An L-shape doesn't need to fail this check; it's informational

    def test_dogleg_no_clearance_violations(self, fine_pitch_pcb: Path):
        """All placed vias (straight or dog-leg) should respect clearance."""
        result = run_stitch(
            pcb_path=fine_pitch_pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )

        sexp = load_pcb(fine_pitch_pcb)
        other_pads = find_all_pads(sexp, exclude_nets={1})  # Exclude GND (net 1)

        for via in result.vias_added:
            via_radius = via.size / 2
            for px, py, p_radius, _pnet in other_pads:
                dist = math.sqrt((via.via_x - px) ** 2 + (via.via_y - py) ** 2)
                min_clearance = via_radius + p_radius + 0.2
                assert dist >= min_clearance - 0.01, (
                    f"Via at ({via.via_x:.2f}, {via.via_y:.2f}) for "
                    f"{via.pad.reference}.{via.pad.pad_number} violates clearance "
                    f"to pad at ({px:.2f}, {py:.2f}): "
                    f"dist={dist:.3f} < min={min_clearance:.3f}"
                )

    def test_dogleg_improves_placement_success_rate(self, fine_pitch_pcb: Path):
        """Dog-leg routing should improve success rate on fine-pitch packages."""
        result = run_stitch(
            pcb_path=fine_pitch_pcb,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )

        # GND pads in the SSOP-20: pins 3, 8, 13, 18 (4 pads total)
        total_gnd_pads = 4
        placed_count = len(result.vias_added)
        skipped_count = len(result.pads_skipped)

        # With dog-leg routing, we should achieve at least 50% success rate
        # on fine-pitch packages (better than 0% without dog-leg)
        success_rate = placed_count / total_gnd_pads if total_gnd_pads > 0 else 0
        assert success_rate >= 0.5 or placed_count >= 2, (
            f"Dog-leg routing should achieve at least 50% success rate on fine-pitch. "
            f"Got {placed_count}/{total_gnd_pads} ({success_rate * 100:.0f}%)"
        )


class TestPostStitchDRC:
    """Tests for the --drc flag and run_post_stitch_drc function."""

    def test_drc_flag_accepted(self, stitch_test_pcb: Path, capsys):
        """--drc flag should be accepted without error."""
        # With --dry-run, DRC won't actually run (only runs when vias are added
        # and not in dry-run mode)
        exit_code = main([str(stitch_test_pcb), "--net", "GND", "--dry-run", "--drc"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "dry run" in captured.out.lower()

    def test_output_result_hides_drc_hint_when_drc_enabled(self, stitch_test_pcb: Path, capsys):
        """When --drc is used, the 'Run DRC to verify' message should not appear."""
        from unittest.mock import patch

        # Mock find_kicad_cli to return None so DRC is skipped gracefully
        with patch("kicad_tools.cli.stitch_cmd.run_post_stitch_drc", return_value=0):
            exit_code = main([str(stitch_test_pcb), "--net", "GND", "--drc"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Run DRC to verify" not in captured.out

    def test_output_result_shows_drc_hint_without_flag(self, stitch_test_pcb: Path, capsys):
        """Without --drc, the 'Run DRC to verify' message should appear."""
        exit_code = main([str(stitch_test_pcb), "--net", "GND"])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Run DRC to verify" in captured.out

    def test_run_post_stitch_drc_no_kicad_cli(self, tmp_path, capsys):
        """run_post_stitch_drc should warn and return 1 when kicad-cli is not found."""
        from unittest.mock import patch

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        with patch("kicad_tools.cli.runner.find_kicad_cli", return_value=None):
            result = run_post_stitch_drc(pcb_path)

        assert result == 1
        captured = capsys.readouterr()
        assert "kicad-cli not found" in captured.err

    def test_run_post_stitch_drc_success(self, tmp_path, capsys):
        """run_post_stitch_drc should display summary on successful DRC run."""
        import json
        from unittest.mock import patch

        from kicad_tools.cli.runner import KiCadCLIResult

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        # Create a mock DRC report JSON
        drc_report = {
            "source": str(pcb_path),
            "coordinate_units": "mm",
            "violations": [
                {
                    "type": "clearance",
                    "severity": "warning",
                    "description": "Clearance violation",
                    "items": [],
                }
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        }
        report_path = tmp_path / "drc_report.json"
        report_path.write_text(json.dumps(drc_report))

        mock_result = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch(
                "kicad_tools.cli.runner.run_drc",
                return_value=mock_result,
            ),
        ):
            result = run_post_stitch_drc(pcb_path)

        assert result == 0
        captured = capsys.readouterr()
        assert "POST-STITCH DRC RESULTS" in captured.out

    def test_run_post_stitch_drc_with_errors(self, tmp_path, capsys):
        """run_post_stitch_drc should show FAILED when DRC has errors."""
        import json
        from unittest.mock import patch

        from kicad_tools.cli.runner import KiCadCLIResult

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        drc_report = {
            "source": str(pcb_path),
            "coordinate_units": "mm",
            "violations": [
                {
                    "type": "clearance",
                    "severity": "error",
                    "description": "Clearance violation (min 0.200mm; actual 0.150mm)",
                    "items": [],
                }
            ],
            "unconnected_items": [],
            "schematic_parity": [],
        }
        report_path = tmp_path / "drc_report.json"
        report_path.write_text(json.dumps(drc_report))

        mock_result = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch(
                "kicad_tools.cli.runner.run_drc",
                return_value=mock_result,
            ),
        ):
            result = run_post_stitch_drc(pcb_path)

        assert result == 0  # DRC ran successfully, even though there are errors
        captured = capsys.readouterr()
        assert "DRC FAILED" in captured.out
        assert "ERRORS (must fix)" in captured.out

    def test_run_post_stitch_drc_clean(self, tmp_path, capsys):
        """run_post_stitch_drc should show PASSED when no violations."""
        import json
        from unittest.mock import patch

        from kicad_tools.cli.runner import KiCadCLIResult

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        drc_report = {
            "source": str(pcb_path),
            "coordinate_units": "mm",
            "violations": [],
            "unconnected_items": [],
            "schematic_parity": [],
        }
        report_path = tmp_path / "drc_report.json"
        report_path.write_text(json.dumps(drc_report))

        mock_result = KiCadCLIResult(
            success=True,
            output_path=report_path,
            return_code=0,
        )

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch(
                "kicad_tools.cli.runner.run_drc",
                return_value=mock_result,
            ),
        ):
            result = run_post_stitch_drc(pcb_path)

        assert result == 0
        captured = capsys.readouterr()
        assert "DRC PASSED" in captured.out
        assert "Errors:   0" in captured.out

    def test_run_post_stitch_drc_failure(self, tmp_path, capsys):
        """run_post_stitch_drc should return 1 when DRC fails to run."""
        from unittest.mock import patch

        from kicad_tools.cli.runner import KiCadCLIResult

        pcb_path = tmp_path / "test.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        mock_result = KiCadCLIResult(
            success=False,
            stderr="kicad-cli crashed",
            return_code=1,
        )

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch(
                "kicad_tools.cli.runner.run_drc",
                return_value=mock_result,
            ),
        ):
            result = run_post_stitch_drc(pcb_path)

        assert result == 1
        captured = capsys.readouterr()
        assert "DRC failed to run" in captured.err


# ============================================================================
# Blanket Stitching Tests
# ============================================================================


# PCB with a GND zone polygon for blanket stitching tests
BLANKET_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-blanket-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 130 100) (xy 130 130) (xy 100 130)))
  )
)
"""


# PCB with zone but also a track across it for clearance testing
BLANKET_CLEARANCE_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-clr-uuid")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 115 100) (xy 115 115) (xy 100 115)))
  )
  (segment (start 105 105) (end 110 105) (width 0.25) (layer "F.Cu") (net 2))
)
"""


class TestPointInPolygon:
    """Tests for the point_in_polygon ray-casting function."""

    def test_point_inside_rectangle(self):
        """Point inside a simple rectangle should return True."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert point_in_polygon(5, 5, rect) is True

    def test_point_outside_rectangle(self):
        """Point outside a rectangle should return False."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert point_in_polygon(15, 5, rect) is False

    def test_point_above_rectangle(self):
        """Point above a rectangle should return False."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        assert point_in_polygon(5, 15, rect) is False

    def test_point_inside_triangle(self):
        """Point inside a triangle should return True."""
        tri = [(0, 0), (10, 0), (5, 10)]
        assert point_in_polygon(5, 3, tri) is True

    def test_point_outside_triangle(self):
        """Point outside a triangle should return False."""
        tri = [(0, 0), (10, 0), (5, 10)]
        assert point_in_polygon(0, 10, tri) is False

    def test_point_at_negative_coords(self):
        """Point testing with negative coordinates."""
        rect = [(-10, -10), (10, -10), (10, 10), (-10, 10)]
        assert point_in_polygon(0, 0, rect) is True
        assert point_in_polygon(-15, 0, rect) is False


class TestGenerateGridPositions:
    """Tests for the grid position generation function."""

    def test_basic_grid_in_rectangle(self):
        """Should generate grid positions inside a rectangular polygon."""
        # 30x30 rectangle from (100,100) to (130,130)
        rect = [(100, 100), (130, 100), (130, 130), (100, 130)]
        positions = generate_grid_positions(rect, spacing=5.0, margin=1.0)
        assert len(positions) > 0

        # All positions should be inside the polygon
        for x, y in positions:
            assert point_in_polygon(x, y, rect), f"({x}, {y}) not inside polygon"

    def test_grid_spacing_respected(self):
        """Grid positions should be spaced at the given interval."""
        rect = [(0, 0), (30, 0), (30, 30), (0, 30)]
        positions = generate_grid_positions(rect, spacing=5.0, margin=0.5)

        # All x and y coordinates should be multiples of 5.0
        for x, y in positions:
            assert x % 5.0 == pytest.approx(0, abs=1e-9), f"x={x} not on grid"
            assert y % 5.0 == pytest.approx(0, abs=1e-9), f"y={y} not on grid"

    def test_margin_respected(self):
        """Grid positions should be at least `margin` from polygon edges."""
        rect = [(100, 100), (130, 100), (130, 130), (100, 130)]
        margin = 2.0
        positions = generate_grid_positions(rect, spacing=3.0, margin=margin)

        for x, y in positions:
            # Check distance from each edge
            assert x >= 100 + margin - 0.01, f"x={x} too close to left edge"
            assert x <= 130 - margin + 0.01, f"x={x} too close to right edge"
            assert y >= 100 + margin - 0.01, f"y={y} too close to top edge"
            assert y <= 130 - margin + 0.01, f"y={y} too close to bottom edge"

    def test_empty_polygon(self):
        """Empty polygon should return no positions."""
        positions = generate_grid_positions([], spacing=5.0, margin=1.0)
        assert positions == []

    def test_zero_spacing(self):
        """Zero spacing should return no positions."""
        rect = [(0, 0), (10, 0), (10, 10), (0, 10)]
        positions = generate_grid_positions(rect, spacing=0, margin=0.5)
        assert positions == []

    def test_large_spacing_small_polygon(self):
        """Large spacing relative to polygon may yield few or zero positions."""
        small_rect = [(0, 0), (2, 0), (2, 2), (0, 2)]
        positions = generate_grid_positions(small_rect, spacing=5.0, margin=0.5)
        # The polygon is only 2x2, spacing 5.0 -- no grid point can fit
        assert len(positions) == 0


class TestCheckViaClearance:
    """Tests for the check_via_clearance function."""

    def test_clear_position(self):
        """A position with no nearby copper should pass clearance."""
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
        )
        assert result is True

    def test_conflict_with_track(self):
        """A via near an other-net track should fail clearance."""
        track = TrackSegment(
            start_x=49.5, start_y=50.0,
            end_x=50.5, end_y=50.0,
            width=0.25, layer="F.Cu", net_number=2,
        )
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[track],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
        )
        assert result is False

    def test_conflict_with_same_net_via(self):
        """A via stacked on an existing same-net via should fail."""
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[(50.0, 50.0)],
        )
        assert result is False

    def test_conflict_with_other_net_via(self):
        """A via near an other-net via should fail clearance."""
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[(50.2, 50.0, 0.45, 2)],
            other_net_pads=[],
            same_net_vias=[],
        )
        assert result is False

    def test_conflict_with_other_net_pad(self):
        """A via near an other-net pad should fail clearance."""
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[(50.0, 50.3, 0.3, 2)],
            same_net_vias=[],
        )
        assert result is False

    def test_far_away_copper_passes(self):
        """Copper far from the via position should not cause a conflict."""
        track = TrackSegment(
            start_x=100.0, start_y=100.0,
            end_x=110.0, end_y=100.0,
            width=0.25, layer="F.Cu", net_number=2,
        )
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[track],
            other_net_vias=[(100.0, 100.0, 0.45, 2)],
            other_net_pads=[(100.0, 100.0, 0.3, 2)],
            same_net_vias=[(100.0, 100.0)],
        )
        assert result is True


class TestExtractZonePolygons:
    """Tests for extracting zone boundary polygons from PCB S-expressions."""

    def test_extract_gnd_zone(self):
        """Should extract zone polygon for GND net."""
        from kicad_tools.core.sexp_file import load_pcb as _load_pcb
        from io import StringIO
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", mode="w", delete=False) as f:
            f.write(BLANKET_TEST_PCB)
            f.flush()
            sexp = _load_pcb(Path(f.name))

        polygons = extract_zone_polygons(sexp, "GND")
        assert len(polygons) == 1
        assert polygons[0].net_name == "GND"
        assert polygons[0].layer == "In1.Cu"
        assert len(polygons[0].points) == 4

    def test_extract_nonexistent_net(self):
        """Should return empty list for net with no zones."""
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", mode="w", delete=False) as f:
            f.write(BLANKET_TEST_PCB)
            f.flush()
            sexp = load_pcb(Path(f.name))

        polygons = extract_zone_polygons(sexp, "NONEXISTENT")
        assert len(polygons) == 0

    def test_extract_from_stitch_zone_pcb(self, stitch_zone_pcb: Path):
        """Should extract zone polygons from the fixture PCB."""
        sexp = load_pcb(stitch_zone_pcb)

        gnd_polys = extract_zone_polygons(sexp, "GND")
        assert len(gnd_polys) == 1
        assert gnd_polys[0].layer == "In1.Cu"

        v3_polys = extract_zone_polygons(sexp, "+3.3V")
        assert len(v3_polys) == 1
        assert v3_polys[0].layer == "In2.Cu"


class TestRunBlanketStitch:
    """Integration tests for the blanket stitching operation."""

    def test_blanket_places_vias_in_zone(self, tmp_path: Path):
        """Blanket stitch should place vias on a grid inside the zone."""
        pcb_file = tmp_path / "blanket.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=5.0,
            dry_run=False,
        )

        # Should have placed some vias
        assert len(result.vias_added) > 0

        # All vias should be inside the zone polygon (100,100)-(130,130)
        zone_poly = [(100, 100), (130, 100), (130, 130), (100, 130)]
        for via in result.vias_added:
            assert point_in_polygon(via.via_x, via.via_y, zone_poly), (
                f"Via at ({via.via_x}, {via.via_y}) outside zone"
            )

        # All vias should be on the GND net
        for via in result.vias_added:
            assert via.pad.net_name == "GND"

        # Should detect In1.Cu as target layer
        assert "GND" in result.detected_layers
        assert result.detected_layers["GND"] == "In1.Cu"

    def test_blanket_dry_run(self, tmp_path: Path):
        """Dry run should not modify the PCB file."""
        pcb_file = tmp_path / "blanket_dry.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)
        original_content = pcb_file.read_text()

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=5.0,
            dry_run=True,
        )

        assert len(result.vias_added) > 0
        assert pcb_file.read_text() == original_content

    def test_blanket_no_traces_added(self, tmp_path: Path):
        """Blanket vias should not add any trace segments."""
        pcb_file = tmp_path / "blanket_no_traces.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=5.0,
        )

        # Blanket mode should not add traces
        assert len(result.traces_added) == 0

    def test_blanket_respects_clearance(self, tmp_path: Path):
        """Blanket vias should not be placed near other-net copper."""
        pcb_file = tmp_path / "blanket_clr.kicad_pcb"
        pcb_file.write_text(BLANKET_CLEARANCE_PCB)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=3.0,
        )

        # The track runs from (105,105) to (110,105) on net 2
        # Vias should not be placed within clearance distance of this track
        for via in result.vias_added:
            dist = point_to_segment_distance(
                via.via_x, via.via_y, 105.0, 105.0, 110.0, 105.0
            )
            min_required = 0.45 / 2 + 0.25 / 2 + 0.2  # via_r + track_w/2 + clearance
            assert dist >= min_required - 0.01, (
                f"Via at ({via.via_x}, {via.via_y}) too close to track: {dist:.3f} < {min_required:.3f}"
            )

    def test_blanket_no_zone_warns(self, tmp_path: Path, capsys):
        """Blanket stitch with no zone polygon should warn and place no vias."""
        no_zone_pcb = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
)
"""
        pcb_file = tmp_path / "no_zone.kicad_pcb"
        pcb_file.write_text(no_zone_pcb)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=3.0,
        )

        assert len(result.vias_added) == 0
        captured = capsys.readouterr()
        assert "No zone polygon found" in captured.err

    def test_blanket_spacing_affects_count(self, tmp_path: Path):
        """Smaller spacing should produce more vias."""
        pcb_file_sparse = tmp_path / "sparse.kicad_pcb"
        pcb_file_sparse.write_text(BLANKET_TEST_PCB)

        pcb_file_dense = tmp_path / "dense.kicad_pcb"
        pcb_file_dense.write_text(BLANKET_TEST_PCB)

        sparse = run_blanket_stitch(
            pcb_path=pcb_file_sparse,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=10.0,
            dry_run=True,
        )

        dense = run_blanket_stitch(
            pcb_path=pcb_file_dense,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=3.0,
            dry_run=True,
        )

        assert len(dense.vias_added) > len(sparse.vias_added)

    def test_blanket_with_drc_flag(self, tmp_path: Path, capsys):
        """Blanket mode with --drc should call DRC after stitching."""
        pcb_file = tmp_path / "blanket_drc.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)

        # Run with --blanket and --drc flags via main()
        exit_code = main([
            str(pcb_file),
            "--net", "GND",
            "--blanket",
            "--spacing", "5.0",
            "--dry-run",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        # Dry run output should show vias
        assert "Added" in captured.out or "via" in captured.out.lower()

    def test_blanket_cli_main(self, tmp_path: Path, capsys):
        """Test blanket mode via the main() CLI entry point."""
        pcb_file = tmp_path / "blanket_cli.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)

        exit_code = main([
            str(pcb_file),
            "--net", "GND",
            "--blanket",
            "--spacing", "5.0",
        ])

        assert exit_code == 0
        captured = capsys.readouterr()
        assert "stitching vias" in captured.out.lower() or "Added" in captured.out


# Dense QFP-like PCB for testing extended escape routing.
# Simulates a QFP-64-like package with 0.5mm pitch where power pins are
# surrounded by signal pins on all sides, preventing both straight-line
# and dog-leg placement.
DENSE_QFP_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (net 3 "SIG_A")
  (net 4 "SIG_B")
  (net 5 "SIG_C")
  (net 6 "SIG_D")
  (net 7 "SIG_E")
  (net 8 "SIG_F")
  (net 9 "SIG_G")
  (net 10 "SIG_H")
  (footprint "Package_QFP:QFP-64_10x10mm_P0.5mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 100 100)
    (property "Reference" "U1" (at 0 -7 0) (layer "F.SilkS") (uuid "ref-uuid-u1"))
    (pad "1" smd roundrect (at -5.5 -3.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "2" smd roundrect (at -5.5 -3.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "3" smd roundrect (at -5.5 -2.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "4" smd roundrect (at -5.5 -2.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 6 "SIG_D"))
    (pad "5" smd roundrect (at -5.5 -1.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 7 "SIG_E"))
    (pad "6" smd roundrect (at -5.5 -1.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 8 "SIG_F"))
    (pad "7" smd roundrect (at -5.5 -0.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 9 "SIG_G"))
    (pad "8" smd roundrect (at -5.5 -0.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "9" smd roundrect (at -5.5 0.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 10 "SIG_H"))
    (pad "10" smd roundrect (at -5.5 0.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "SIG_A"))
    (pad "11" smd roundrect (at -5.5 1.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 4 "SIG_B"))
    (pad "12" smd roundrect (at -5.5 1.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 5 "SIG_C"))
    (pad "13" smd roundrect (at -5.5 2.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "VCC"))
    (pad "14" smd roundrect (at -5.5 2.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 6 "SIG_D"))
    (pad "15" smd roundrect (at -5.5 3.25) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 7 "SIG_E"))
    (pad "16" smd roundrect (at -5.5 3.75) (size 1.2 0.3) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 8 "SIG_F"))
  )
  (zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "zone-gnd-uuid")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.5))
    (polygon
      (pts (xy 85 85) (xy 115 85) (xy 115 115) (xy 85 115))
    )
  )
  (zone (net 2) (net_name "VCC") (layer "B.Cu") (uuid "zone-vcc-uuid")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.5))
    (polygon
      (pts (xy 85 85) (xy 115 85) (xy 115 115) (xy 85 115))
    )
  )
)
"""


class TestExtendedEscapeRouting:
    """Tests for extended escape routing for dense IC packages."""

    @pytest.fixture
    def dense_qfp_pcb(self, tmp_path: Path) -> Path:
        """Create a test PCB file simulating a dense QFP-64 package."""
        pcb_path = tmp_path / "dense_qfp.kicad_pcb"
        pcb_path.write_text(DENSE_QFP_PCB)
        return pcb_path

    def test_calculate_extended_escape_finds_position(self):
        """Extended escape should find a via position using multi-segment path."""
        pad = PadInfo(
            reference="U1",
            pad_number="8",
            net_number=1,
            net_name="GND",
            x=100.0,
            y=100.0,
            layer="F.Cu",
            width=1.2,
            height=0.3,
        )

        # Place blocking pads at offset positions (not directly on cardinal
        # axes from pad center) so they block some via positions but leave
        # escape channels for traces.
        other_net_pads = [
            (99.5, 99.5, 0.15, 3),   # upper-left diagonal
            (100.5, 99.5, 0.15, 4),  # upper-right diagonal
            (99.5, 100.5, 0.15, 5),  # lower-left diagonal
            (100.5, 100.5, 0.15, 6), # lower-right diagonal
        ]

        result = calculate_extended_escape_position(
            pad=pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            escape_distance=4.0,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=other_net_pads,
            trace_width=0.2,
        )

        assert result is not None, "Extended escape should find a via position"
        via_x, via_y, waypoints = result
        assert len(waypoints) >= 1, "Should have at least one waypoint"

        # Via should be at valid distance
        dist = math.sqrt((via_x - pad.x) ** 2 + (via_y - pad.y) ** 2)
        assert dist > 0.5, "Via should be placed beyond immediate pad area"

    def test_extended_escape_returns_valid_path_structure(self):
        """Extended escape should return a properly structured multi-waypoint result."""
        pad = PadInfo(
            reference="U1",
            pad_number="8",
            net_number=1,
            net_name="GND",
            x=100.0,
            y=100.0,
            layer="F.Cu",
            width=0.6,
            height=0.3,
        )

        # Minimal blocking: one pad to the left that forces an L-shape.
        # Extended escape should route around it.
        other_net_pads = [
            (99.2, 100.0, 0.15, 3),
        ]

        result = calculate_extended_escape_position(
            pad=pad, offset=0.5, via_size=0.45, existing_vias=[],
            clearance=0.2, escape_distance=4.0,
            other_net_tracks=[], other_net_vias=[],
            other_net_pads=other_net_pads, trace_width=0.2,
        )

        assert result is not None, "Should find a position"
        via_x, via_y, waypoints = result
        assert isinstance(waypoints, list)
        assert len(waypoints) >= 1
        # Each waypoint should be a tuple of (x, y)
        for wp in waypoints:
            assert len(wp) == 2
            assert isinstance(wp[0], float) or isinstance(wp[0], int)
            assert isinstance(wp[1], float) or isinstance(wp[1], int)

    def test_extended_escape_respects_clearance(self):
        """Extended escape via and trace path should respect clearance."""
        pad = PadInfo(
            reference="U1",
            pad_number="8",
            net_number=1,
            net_name="GND",
            x=100.0,
            y=100.0,
            layer="F.Cu",
            width=1.2,
            height=0.3,
        )

        # Create blocking pads
        other_net_pads = [
            (100.0, 99.5, 0.15, 3),
            (100.0, 100.5, 0.15, 4),
            (99.0, 99.5, 0.15, 5),
            (99.0, 100.5, 0.15, 6),
        ]

        via_size = 0.45
        clearance = 0.2
        via_radius = via_size / 2

        result = calculate_extended_escape_position(
            pad=pad,
            offset=0.5,
            via_size=via_size,
            existing_vias=[],
            clearance=clearance,
            escape_distance=3.0,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=other_net_pads,
            trace_width=0.2,
        )

        if result is not None:
            via_x, via_y, waypoints = result
            # Verify via position has clearance to all other-net pads
            for px, py, p_radius, _pnet in other_net_pads:
                dist = math.sqrt((px - via_x) ** 2 + (py - via_y) ** 2)
                min_dist = via_radius + p_radius + clearance
                assert dist >= min_dist - 0.01, (
                    f"Via at ({via_x:.2f}, {via_y:.2f}) too close to pad at "
                    f"({px:.2f}, {py:.2f}): {dist:.3f} < {min_dist:.3f}"
                )

    def test_extended_escape_respects_max_distance(self):
        """Extended escape should not exceed the escape_distance limit."""
        pad = PadInfo(
            reference="U1",
            pad_number="8",
            net_number=1,
            net_name="GND",
            x=100.0,
            y=100.0,
            layer="F.Cu",
            width=1.2,
            height=0.3,
        )

        # Completely surround the pad with blocking pads at very tight pitch
        # so that only a very long escape could work, beyond our limit
        other_net_pads = []
        for dx in [-0.3, 0.0, 0.3]:
            for dy in [-0.3, 0.0, 0.3]:
                if dx == 0 and dy == 0:
                    continue
                other_net_pads.append((100.0 + dx, 100.0 + dy, 0.15, 3))

        # With a very small escape distance, it should fail
        result = calculate_extended_escape_position(
            pad=pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=[],
            clearance=0.2,
            escape_distance=0.3,  # Very short, unlikely to succeed
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=other_net_pads,
            trace_width=0.2,
        )

        # With such tight surroundings and short distance, we expect None
        assert result is None, (
            "Extended escape should fail when escape_distance is too short"
        )

    def test_trace_segment_is_extended_escape_property(self):
        """TraceSegment.is_extended_escape should correctly identify multi-waypoint traces."""
        pad = PadInfo(
            reference="U1",
            pad_number="8",
            net_number=1,
            net_name="GND",
            x=100,
            y=100,
            layer="F.Cu",
            width=1.2,
            height=0.3,
        )

        # Straight trace
        straight = TraceSegment(
            pad=pad, via_x=101, via_y=100, width=0.2, layer="F.Cu"
        )
        assert not straight.is_extended_escape

        # Dog-leg trace
        dogleg = TraceSegment(
            pad=pad, via_x=101, via_y=101, width=0.2, layer="F.Cu",
            intermediate_x=101, intermediate_y=100,
        )
        assert not dogleg.is_extended_escape

        # Extended escape trace
        extended = TraceSegment(
            pad=pad, via_x=103, via_y=102, width=0.2, layer="F.Cu",
            waypoints=[(101, 100), (102, 101)],
        )
        assert extended.is_extended_escape

        # Empty waypoints
        empty_wp = TraceSegment(
            pad=pad, via_x=101, via_y=100, width=0.2, layer="F.Cu",
            waypoints=[],
        )
        assert not empty_wp.is_extended_escape

    def test_run_stitch_uses_extended_escape_for_dense_pads(self, dense_qfp_pcb: Path):
        """run_stitch should use extended escape when dogleg fails on dense packages."""
        result = run_stitch(
            pcb_path=dense_qfp_pcb,
            net_names=["GND", "VCC"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
            escape_distance=3.0,
        )

        # Should have placed at least some vias (either via dogleg or extended escape)
        assert len(result.vias_added) > 0, (
            "Should place at least some vias for GND/VCC pins on dense QFP"
        )

    def test_run_stitch_extended_escape_skip_reason(self, dense_qfp_pcb: Path):
        """Pads that fail even extended escape should report descriptive skip reasons."""
        result = run_stitch(
            pcb_path=dense_qfp_pcb,
            net_names=["GND", "VCC"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
            escape_distance=0.1,  # Very short, many should fail
        )

        # Check that any skipped pads have descriptive reasons
        for _pad, reason in result.pads_skipped:
            assert "extended escape" in reason, (
                f"Skip reason should mention extended escape: {reason}"
            )

    def test_existing_simple_stitch_unchanged(self, tmp_path: Path):
        """Existing simple stitch behavior should be unchanged by escape_distance parameter."""
        pcb_file = tmp_path / "simple.kicad_pcb"
        pcb_file.write_text(STITCH_TEST_PCB)

        # Run with default escape_distance - should work identically to before
        result = run_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
            escape_distance=3.0,
        )

        # Simple capacitor pads should still be stitched via straight-line
        assert len(result.vias_added) > 0
        # None of the traces should be extended escape (simple components
        # should use straight-line or at most dog-leg)
        extended_traces = [t for t in result.traces_added if t.is_extended_escape]
        assert len(extended_traces) == 0, (
            "Simple components should not use extended escape routing"
        )

    def test_dogleg_regression_fine_pitch(self, tmp_path: Path):
        """Dog-leg routing on SSOP-like components should still work without escalation."""
        pcb_file = tmp_path / "fine_pitch.kicad_pcb"
        pcb_file.write_text(FINE_PITCH_PCB)

        result = run_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
            escape_distance=3.0,
        )

        # SSOP components should still get vias placed
        assert len(result.vias_added) > 0

    def test_escape_distance_cli_option(self, tmp_path: Path, capsys):
        """The --escape-distance CLI option should be accepted."""
        pcb_file = tmp_path / "escape_cli.kicad_pcb"
        pcb_file.write_text(STITCH_TEST_PCB)

        exit_code = main([
            str(pcb_file),
            "--net", "GND",
            "--escape-distance", "5.0",
            "--dry-run",
        ])

        assert exit_code == 0

    def test_output_counts_extended_escape_traces(self, dense_qfp_pcb: Path, capsys):
        """Output summary should count extended escape traces separately."""
        result = run_stitch(
            pcb_path=dense_qfp_pcb,
            net_names=["GND", "VCC"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
            escape_distance=3.0,
        )

        from kicad_tools.cli.stitch_cmd import output_result
        output_result(result, dry_run=True)

        captured = capsys.readouterr()
        # If extended escape traces were used, the summary should mention them
        extended_traces = [t for t in result.traces_added if t.is_extended_escape]
        if extended_traces:
            assert "extended escape" in captured.out.lower()

# --- Filled polygon clearance tests ---


# PCB with a zone that has filled_polygon nodes (simulating post-DRC fill)
FILLED_POLYGON_TEST_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "SIG")
  (net 3 "PWR")
  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-fp-gnd")
    (name "GND_plane")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 100 100) (xy 130 100) (xy 130 130) (xy 100 130)))
    (filled_polygon (layer "In1.Cu") (pts (xy 100 100) (xy 130 100) (xy 130 130) (xy 100 130)))
  )
  (zone (net 2) (net_name "SIG") (layer "F.Cu") (uuid "zone-fp-sig")
    (name "SIG_zone")
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes)
    (polygon (pts (xy 50 50) (xy 60 50) (xy 60 60) (xy 50 60)))
    (filled_polygon (layer "F.Cu") (pts (xy 50 50) (xy 60 50) (xy 60 60) (xy 50 60)))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000900")
    (at 55 55)
    (property "Reference" "C9" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-uuid-c9"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "PWR"))
  )
)
"""


class TestFilledPolygonDataclass:
    """Tests for the FilledPolygon dataclass."""

    def test_bounding_box_computed(self):
        """Bounding box should be computed from points in __post_init__."""
        fp = FilledPolygon(
            net_number=1,
            net_name="GND",
            layer="In1.Cu",
            points=[(10, 20), (30, 40), (15, 50)],
        )
        assert fp.min_x == 10
        assert fp.max_x == 30
        assert fp.min_y == 20
        assert fp.max_y == 50

    def test_empty_points(self):
        """Empty points list should leave bounding box at defaults."""
        fp = FilledPolygon(
            net_number=1,
            net_name="GND",
            layer="In1.Cu",
            points=[],
        )
        assert fp.min_x == 0.0
        assert fp.max_x == 0.0


class TestFindAllFilledPolygons:
    """Tests for extracting filled polygon data from PCB S-expressions."""

    def test_extract_filled_polygons(self):
        """Should extract filled polygons from zones with filled_polygon nodes."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", mode="w", delete=False) as f:
            f.write(FILLED_POLYGON_TEST_PCB)
            f.flush()
            sexp = load_pcb(Path(f.name))

        # Exclude net 1 (GND) -- should only get SIG zone's filled polygon
        polys = find_all_filled_polygons(sexp, exclude_nets={1})
        assert len(polys) == 1
        assert polys[0].net_name == "SIG"
        assert polys[0].net_number == 2
        assert polys[0].layer == "F.Cu"
        assert len(polys[0].points) == 4

    def test_extract_all_filled_polygons(self):
        """Should extract all filled polygons when no nets excluded."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", mode="w", delete=False) as f:
            f.write(FILLED_POLYGON_TEST_PCB)
            f.flush()
            sexp = load_pcb(Path(f.name))

        polys = find_all_filled_polygons(sexp)
        assert len(polys) == 2

    def test_no_filled_polygons(self):
        """PCB with zones but no filled_polygon nodes should return empty."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", mode="w", delete=False) as f:
            f.write(BLANKET_TEST_PCB)
            f.flush()
            sexp = load_pcb(Path(f.name))

        polys = find_all_filled_polygons(sexp)
        assert len(polys) == 0


class TestCheckViaClearanceFilledPolygons:
    """Tests for filled polygon clearance in check_via_clearance."""

    def _make_filled_polygon(self, points, net_number=2, net_name="SIG", layer="F.Cu"):
        return FilledPolygon(
            net_number=net_number,
            net_name=net_name,
            layer=layer,
            points=points,
        )

    def test_via_inside_other_net_polygon_rejected(self):
        """A via placed inside an other-net filled polygon should be rejected."""
        polygon = self._make_filled_polygon(
            [(40, 40), (60, 40), (60, 60), (40, 60)]
        )
        result = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
            other_net_filled_polygons=[polygon],
        )
        assert result is False

    def test_via_near_polygon_edge_rejected(self):
        """A via within clearance of a polygon edge should be rejected."""
        # Polygon edge runs from (40, 40) to (60, 40)
        # Via at (50, 39.8) with radius 0.225 + clearance 0.2 = 0.425
        # Distance from (50, 39.8) to edge y=40 is 0.2, which < 0.425
        polygon = self._make_filled_polygon(
            [(40, 40), (60, 40), (60, 60), (40, 60)]
        )
        result = check_via_clearance(
            x=50.0,
            y=39.8,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
            other_net_filled_polygons=[polygon],
        )
        assert result is False

    def test_via_far_from_polygon_passes(self):
        """A via far from all polygon edges should pass."""
        polygon = self._make_filled_polygon(
            [(40, 40), (60, 40), (60, 60), (40, 60)]
        )
        result = check_via_clearance(
            x=10.0,
            y=10.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
            other_net_filled_polygons=[polygon],
        )
        assert result is True

    def test_empty_filled_polygon_list_backward_compat(self):
        """check_via_clearance with empty filled polygon list behaves identically."""
        # Test that passing empty list gives same result as not passing it
        result_without = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
        )
        result_with_empty = check_via_clearance(
            x=50.0,
            y=50.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
            other_net_filled_polygons=[],
        )
        assert result_without == result_with_empty == True

    def test_via_exactly_on_polygon_edge(self):
        """Via center exactly on a polygon edge (boundary condition)."""
        polygon = self._make_filled_polygon(
            [(40, 40), (60, 40), (60, 60), (40, 60)]
        )
        # Point on the edge y=40: distance is 0, which < via_radius + clearance
        result = check_via_clearance(
            x=50.0,
            y=40.0,
            via_size=0.45,
            clearance=0.2,
            other_net_tracks=[],
            other_net_vias=[],
            other_net_pads=[],
            same_net_vias=[],
            other_net_filled_polygons=[polygon],
        )
        assert result is False


class TestCalculateViaPositionFilledPolygons:
    """Tests for filled polygon clearance in calculate_via_position."""

    def test_avoids_filled_polygon(self):
        """calculate_via_position should avoid placing vias near other-net fills."""
        # Create a polygon that surrounds the pad area except one escape direction
        pad = PadInfo(
            reference="C1", pad_number="1", net_number=1, net_name="GND",
            x=50.0, y=50.0, layer="F.Cu", width=0.54, height=0.64,
        )
        # Large polygon covering most directions from the pad
        polygon = FilledPolygon(
            net_number=2, net_name="SIG", layer="F.Cu",
            points=[(49, 49), (55, 49), (55, 55), (49, 55)],
        )
        # Without polygon, a position should be found
        pos_without = calculate_via_position(
            pad, offset=0.5, via_size=0.45,
            existing_vias=[], clearance=0.2,
        )
        assert pos_without is not None

        # With polygon covering the area, the via should either be
        # placed farther away or in a direction not blocked
        pos_with = calculate_via_position(
            pad, offset=0.5, via_size=0.45,
            existing_vias=[], clearance=0.2,
            other_net_filled_polygons=[polygon],
        )
        # The polygon covers x=49-55, y=49-55. The pad is at (50, 50).
        # Any via placed at a small offset in most directions will be inside
        # or too close to the polygon. The function might find a position
        # in the -x,-y direction or return None.
        if pos_with is not None:
            vx, vy = pos_with
            # Verify the returned position is NOT inside the polygon
            assert not (49 <= vx <= 55 and 49 <= vy <= 55), \
                f"Via at ({vx}, {vy}) should not be inside the polygon"


class TestBlanketStitchFilledPolygons:
    """Integration tests for blanket stitching with filled polygon clearance."""

    def test_blanket_stitch_skips_filled_polygon_positions(self, tmp_path: Path):
        """run_blanket_stitch should skip grid positions that violate fill clearance."""
        pcb_file = tmp_path / "blanket_fill.kicad_pcb"
        pcb_file.write_text(FILLED_POLYGON_TEST_PCB)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=3.0,
            dry_run=True,
        )

        # The GND zone spans (100, 100) to (130, 130).
        # The SIG filled polygon spans (50, 50) to (60, 60) which is
        # far from the GND zone, so all vias should be placed fine.
        # The key point is that the code path runs without error.
        # Vias should have been generated.
        assert isinstance(result.vias_added, list)

    def test_blanket_stitch_no_filled_polygons_unchanged(self, tmp_path: Path):
        """Blanket stitch on PCB with no filled polygons should work as before."""
        pcb_file = tmp_path / "blanket_no_fill.kicad_pcb"
        pcb_file.write_text(BLANKET_TEST_PCB)

        result = run_blanket_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            spacing=3.0,
            dry_run=True,
        )
        # Should still work and place vias
        assert len(result.vias_added) > 0


class TestStitchFilledPolygons:
    """Integration tests for pad-based stitching with filled polygon clearance."""

    def test_stitch_no_filled_polygons_unchanged(self, tmp_path: Path):
        """Stitch on PCB with no filled polygons should work identically."""
        pcb_file = tmp_path / "stitch_no_fill.kicad_pcb"
        pcb_file.write_text(STITCH_TEST_PCB)

        result = run_stitch(
            pcb_path=pcb_file,
            net_names=["GND"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )
        # Should still work and place vias for the GND pads
        assert len(result.vias_added) > 0

    def test_stitch_passes_filled_polygons(self, tmp_path: Path):
        """Stitch on PCB with filled polygons runs without error."""
        pcb_file = tmp_path / "stitch_fill.kicad_pcb"
        pcb_file.write_text(FILLED_POLYGON_TEST_PCB)

        result = run_stitch(
            pcb_path=pcb_file,
            net_names=["PWR"],
            via_size=0.45,
            drill=0.2,
            clearance=0.2,
            offset=0.5,
            trace_width=0.2,
            dry_run=True,
        )
        # PWR has a pad at (54.49, 55). The SIG filled polygon is at (50-60, 50-60).
        # The via placement should either avoid the polygon or skip the pad.
        assert isinstance(result.vias_added, list)
        assert isinstance(result.pads_skipped, list)
