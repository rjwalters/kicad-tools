"""Tests for the kicad-pcb-stitch CLI command."""

from pathlib import Path

import pytest

from kicad_tools.cli.stitch_cmd import (
    PadInfo,
    calculate_via_position,
    find_existing_tracks,
    find_existing_vias,
    find_pads_on_nets,
    get_net_map,
    get_net_number,
    get_via_layers,
    is_pad_connected,
    main,
    run_stitch,
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
