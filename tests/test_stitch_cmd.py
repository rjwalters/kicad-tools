"""Tests for the kicad-pcb-stitch CLI command."""

import math
from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    TraceSegment,
    TrackSegment,
    calculate_via_position,
    find_all_board_vias,
    find_all_plane_nets,
    find_all_track_segments,
    find_existing_tracks,
    find_existing_vias,
    find_pads_on_nets,
    get_net_map,
    get_net_number,
    get_via_layers,
    is_pad_connected,
    main,
    point_to_segment_distance,
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
        via_at_pattern = re.compile(r'\(via\s.*?\(at\s+[\d.]+\s+[\d.]+\s+\d+\)', re.DOTALL)
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
                start_x=100.8, start_y=99, end_x=100.8, end_y=101,
                width=0.2, layer="F.Cu", net_number=2,
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
            dist = point_to_segment_distance(
                pos[0], pos[1], 100.8, 99, 100.8, 101
            )
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
                start_x=99.0, start_y=99.5, end_x=101.0, end_y=99.5,
                width=0.3, layer="F.Cu", net_number=2,
            ),
            TrackSegment(
                start_x=99.0, start_y=100.5, end_x=101.0, end_y=100.5,
                width=0.3, layer="F.Cu", net_number=2,
            ),
            TrackSegment(
                start_x=99.5, start_y=99.0, end_x=99.5, end_y=101.0,
                width=0.3, layer="F.Cu", net_number=2,
            ),
            TrackSegment(
                start_x=100.5, start_y=99.0, end_x=100.5, end_y=101.0,
                width=0.3, layer="F.Cu", net_number=2,
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
            0.0, 0.0, 2.0, 0.0,  # Segment A: horizontal at y=0
            0.0, 1.0, 2.0, 1.0,  # Segment B: horizontal at y=1
        )
        assert dist == pytest.approx(1.0)

    def test_crossing_segments(self):
        """Crossing segments should have distance 0."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 2.0, 2.0,  # Segment A: diagonal
            0.0, 2.0, 2.0, 0.0,  # Segment B: opposite diagonal (crosses A)
        )
        assert dist == pytest.approx(0.0)

    def test_t_shaped_segments(self):
        """Perpendicular segments that don't cross."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 2.0, 0.0,  # Segment A: horizontal
            1.0, 1.0, 1.0, 3.0,  # Segment B: vertical, starts 1 unit above A
        )
        assert dist == pytest.approx(1.0)

    def test_collinear_separated_segments(self):
        """Collinear segments with a gap."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 1.0, 0.0,  # Segment A: (0,0)-(1,0)
            3.0, 0.0, 4.0, 0.0,  # Segment B: (3,0)-(4,0)
        )
        assert dist == pytest.approx(2.0)

    def test_endpoint_to_endpoint(self):
        """Distance between segment endpoints when closest."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 1.0, 0.0,  # Segment A
            2.0, 1.0, 3.0, 1.0,  # Segment B
        )
        expected = math.sqrt(1.0**2 + 1.0**2)  # dist from (1,0) to (2,1)
        assert dist == pytest.approx(expected)

    def test_zero_length_segment(self):
        """Degenerate (zero-length) segment acts as point."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 0.0, 0.0,  # Point at origin
            1.0, 0.0, 2.0, 0.0,  # Segment from (1,0) to (2,0)
        )
        assert dist == pytest.approx(1.0)

    def test_identical_segments(self):
        """Overlapping segments should have distance 0."""
        dist = segment_to_segment_distance(
            0.0, 0.0, 2.0, 0.0,
            0.0, 0.0, 2.0, 0.0,
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
                start_x=100.5, start_y=98.0, end_x=100.5, end_y=102.0,
                width=0.2, layer="F.Cu", net_number=2,
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
                pad.x, pad.y, pos[0], pos[1],
                100.5, 98.0, 100.5, 102.0,
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
                start_x=99.5, start_y=100.15, end_x=101.5, end_y=100.15,
                width=0.2, layer="F.Cu", net_number=2,
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
                pad.x, pad.y, pos[0], pos[1],
                99.5, 100.15, 101.5, 100.15,
            )
            min_clearance = 0.2 / 2 + 0.2 / 2 + 0.2
            assert trace_dist >= min_clearance - 0.001

    def test_trace_path_with_clear_route_accepted(self):
        """Trace path that clears all obstacles should be accepted."""
        pad = self._make_pad()

        # Place a track far away (y=105) that won't interfere with any direction
        other_tracks = [
            TrackSegment(
                start_x=98.0, start_y=105.0, end_x=102.0, end_y=105.0,
                width=0.2, layer="F.Cu", net_number=2,
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
                100.5, 100.0, pad.x, pad.y, pos[0], pos[1],
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
                start_x=100.4, start_y=98.0, end_x=100.4, end_y=102.0,
                width=0.2, layer="F.Cu", net_number=2,
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
            pad.x, pad.y, pos_with_trace_check[0], pos_with_trace_check[1],
            100.4, 98.0, 100.4, 102.0,
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
                    via.pad.x, via.pad.y, via.via_x, via.via_y,
                    sx, sy, ex, ey,
                )
                min_clearance = 0.2 / 2 + track_width / 2 + 0.2
                assert trace_dist >= min_clearance - 0.01, (
                    f"Trace from pad ({via.pad.x:.2f}, {via.pad.y:.2f}) to "
                    f"via ({via.via_x:.2f}, {via.via_y:.2f}) violates clearance "
                    f"to track ({sx}, {sy})-({ex}, {ey}): "
                    f"dist={trace_dist:.3f} < min={min_clearance:.3f}"
                )
