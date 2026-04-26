"""Tests for PCB summary via and zone count accuracy.

Verifies that ``pcb.summary()``, ``pcb.via_count``, ``pcb.zone_count``,
and ``pcb.segment_count`` always reflect the actual S-expression tree
content, even after in-memory list modifications.

Addresses:
    https://github.com/rjwalters/kicad-tools/issues/1943
    https://github.com/rjwalters/kicad-tools/issues/2064
"""

import json
from pathlib import Path

import pytest

from kicad_tools.schema import PCB


# ---------------------------------------------------------------------------
# Fixture: PCB with known vias, zones, and segments
# ---------------------------------------------------------------------------

PCB_WITH_VIAS_AND_ZONES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.Cu")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Cu")
      (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "+3.3V"))
  )
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000020"))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000021"))
  (segment (start 120 100) (end 130 100) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000022"))
  (via (at 110 100) (size 0.6) (drill 0.3)
    (layers "F.Cu" "B.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000030"))
  (via (at 120 100) (size 0.6) (drill 0.3)
    (layers "F.Cu" "B.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000031"))
  (zone (net 1) (net_name "GND") (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000040")
    (name "GND_pour")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.2)
    (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 90 90) (xy 140 90) (xy 140 110) (xy 90 110)))
  )
)
"""

PCB_NO_VIAS_NO_ZONES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000010")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.Cu")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000011"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Cu")
      (uuid "00000000-0000-0000-0000-000000000012"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "GND"))
  )
)
"""


@pytest.fixture
def pcb_with_vias_zones(tmp_path: Path) -> Path:
    """Create a PCB file with known vias, zones, and segments."""
    pcb_file = tmp_path / "board_with_vias.kicad_pcb"
    pcb_file.write_text(PCB_WITH_VIAS_AND_ZONES)
    return pcb_file


@pytest.fixture
def pcb_no_vias_zones(tmp_path: Path) -> Path:
    """Create a PCB file with no vias or zones."""
    pcb_file = tmp_path / "board_no_vias.kicad_pcb"
    pcb_file.write_text(PCB_NO_VIAS_NO_ZONES)
    return pcb_file


# ---------------------------------------------------------------------------
# Basic count accuracy tests
# ---------------------------------------------------------------------------


class TestSummaryCounts:
    """Verify summary() reports accurate via, zone, and segment counts."""

    def test_via_count_matches_file(self, pcb_with_vias_zones: Path):
        """via_count must match the number of top-level (via ...) nodes."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.via_count == 2

    def test_zone_count_matches_file(self, pcb_with_vias_zones: Path):
        """zone_count must match the number of top-level (zone ...) nodes."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.zone_count == 1

    def test_segment_count_matches_file(self, pcb_with_vias_zones: Path):
        """segment_count must match the number of top-level (segment ...) nodes."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.segment_count == 3

    def test_summary_via_count(self, pcb_with_vias_zones: Path):
        """summary()['vias'] must match via_count."""
        pcb = PCB.load(pcb_with_vias_zones)
        summary = pcb.summary()
        assert summary["vias"] == 2
        assert summary["vias"] == pcb.via_count

    def test_summary_zone_count(self, pcb_with_vias_zones: Path):
        """summary()['zones'] must match zone_count."""
        pcb = PCB.load(pcb_with_vias_zones)
        summary = pcb.summary()
        assert summary["zones"] == 1
        assert summary["zones"] == pcb.zone_count

    def test_summary_segment_count(self, pcb_with_vias_zones: Path):
        """summary()['segments'] must match segment_count."""
        pcb = PCB.load(pcb_with_vias_zones)
        summary = pcb.summary()
        assert summary["segments"] == 3
        assert summary["segments"] == pcb.segment_count

    def test_zero_vias_reported_as_zero(self, pcb_no_vias_zones: Path):
        """A board with no vias must report 0."""
        pcb = PCB.load(pcb_no_vias_zones)
        assert pcb.via_count == 0
        assert pcb.summary()["vias"] == 0

    def test_zero_zones_reported_as_zero(self, pcb_no_vias_zones: Path):
        """A board with no zones must report 0."""
        pcb = PCB.load(pcb_no_vias_zones)
        assert pcb.zone_count == 0
        assert pcb.summary()["zones"] == 0


# ---------------------------------------------------------------------------
# Counts remain accurate after modifications
# ---------------------------------------------------------------------------


class TestCountsAfterModification:
    """Verify counts stay correct after add/strip operations."""

    def test_via_count_after_add_via(self, pcb_no_vias_zones: Path):
        """Adding a via must increment the count."""
        pcb = PCB.load(pcb_no_vias_zones)
        assert pcb.via_count == 0

        pcb.add_via(105, 100, net="GND")
        assert pcb.via_count == 1
        assert pcb.summary()["vias"] == 1

    def test_via_count_after_multiple_adds(self, pcb_no_vias_zones: Path):
        """Adding multiple vias must be reflected accurately."""
        pcb = PCB.load(pcb_no_vias_zones)
        pcb.add_via(105, 100, net="GND")
        pcb.add_via(110, 100, net="GND")
        pcb.add_via(115, 100, net="GND")
        assert pcb.via_count == 3

    def test_segment_count_after_add_trace(self, pcb_no_vias_zones: Path):
        """Adding a trace must increment the segment count."""
        pcb = PCB.load(pcb_no_vias_zones)
        initial = pcb.segment_count
        pcb.add_trace((100, 100), (110, 100), net="GND")
        assert pcb.segment_count == initial + 1

    def test_counts_after_strip_all(self, pcb_with_vias_zones: Path):
        """Stripping all traces must zero out segment and via counts."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.via_count > 0
        assert pcb.segment_count > 0

        pcb.strip_traces(keep_zones=False)
        assert pcb.via_count == 0
        assert pcb.zone_count == 0
        assert pcb.segment_count == 0
        assert pcb.summary()["vias"] == 0
        assert pcb.summary()["zones"] == 0
        assert pcb.summary()["segments"] == 0

    def test_counts_after_strip_keep_zones(self, pcb_with_vias_zones: Path):
        """Stripping traces with keep_zones=True must preserve zone count."""
        pcb = PCB.load(pcb_with_vias_zones)
        original_zones = pcb.zone_count
        assert original_zones > 0

        pcb.strip_traces(keep_zones=True)
        assert pcb.zone_count == original_zones
        assert pcb.via_count == 0
        assert pcb.segment_count == 0


# ---------------------------------------------------------------------------
# S-expression tree is the source of truth (drift resilience)
# ---------------------------------------------------------------------------


class TestSexpSourceOfTruth:
    """Counts must reflect the S-expression tree, not the in-memory cache."""

    def test_via_count_survives_cache_clear(self, pcb_with_vias_zones: Path):
        """Clearing _vias must NOT affect via_count (S-exp is truth)."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.via_count == 2

        # Simulate drift: clear the in-memory list
        pcb._vias.clear()
        # via_count should still report from S-expression tree
        assert pcb.via_count == 2
        assert pcb.summary()["vias"] == 2

    def test_zone_count_survives_cache_clear(self, pcb_with_vias_zones: Path):
        """Clearing _zones must NOT affect zone_count (S-exp is truth)."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.zone_count == 1

        pcb._zones.clear()
        assert pcb.zone_count == 1
        assert pcb.summary()["zones"] == 1

    def test_segment_count_survives_cache_clear(self, pcb_with_vias_zones: Path):
        """Clearing _segments must NOT affect segment_count (S-exp is truth)."""
        pcb = PCB.load(pcb_with_vias_zones)
        assert pcb.segment_count == 3

        pcb._segments.clear()
        assert pcb.segment_count == 3
        assert pcb.summary()["segments"] == 3


# ---------------------------------------------------------------------------
# Round-trip: save and reload
# ---------------------------------------------------------------------------


class TestCountsAfterSaveReload:
    """Counts must be identical after save/reload cycle."""

    def test_counts_survive_save_reload(self, pcb_with_vias_zones: Path, tmp_path: Path):
        """Save and reload must produce identical counts."""
        pcb = PCB.load(pcb_with_vias_zones)
        original_summary = pcb.summary()

        output = tmp_path / "saved.kicad_pcb"
        pcb.save(output)

        pcb2 = PCB.load(output)
        reloaded_summary = pcb2.summary()

        assert reloaded_summary["vias"] == original_summary["vias"]
        assert reloaded_summary["zones"] == original_summary["zones"]
        assert reloaded_summary["segments"] == original_summary["segments"]

    def test_counts_survive_modify_save_reload(
        self, pcb_no_vias_zones: Path, tmp_path: Path
    ):
        """Add vias, save, reload -- counts must match."""
        pcb = PCB.load(pcb_no_vias_zones)
        pcb.add_via(105, 100, net="GND")
        pcb.add_via(110, 100, net="GND")

        output = tmp_path / "modified.kicad_pcb"
        pcb.save(output)

        pcb2 = PCB.load(output)
        assert pcb2.via_count == 2
        assert pcb2.summary()["vias"] == 2


# ---------------------------------------------------------------------------
# Fixture: KiCad 9-style PCB with zone_connect, net_class, groups but no zones
# Regression test for https://github.com/rjwalters/kicad-tools/issues/2064
# ---------------------------------------------------------------------------

PCB_KICAD9_NO_ZONES = """(kicad_pcb
  (version 20240108)
  (generator "kicad")
  (generator_version "9.0.2")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (32 "B.Adhes" user "B.Adhesive")
    (33 "F.Adhes" user "F.Adhesive")
    (34 "B.Paste" user)
    (35 "F.Paste" user)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Mask" user "B.Mask")
    (39 "F.Mask" user "F.Mask")
    (44 "Edge.Cuts" user)
    (46 "B.CrtYd" user "B.Courtyard")
    (47 "F.CrtYd" user "F.Courtyard")
    (48 "B.Fab" user)
    (49 "F.Fab" user)
  )
  (setup
    (pad_to_mask_clearance 0)
    (allow_soldermask_bridges_in_footprints no)
    (pcbplotparams
      (layerselection 0x00010fc_ffffffff)
      (plot_on_all_layers_selection 0x0000000_00000000)
    )
  )
  (net 0 "")
  (net 1 "GND")
  (net 2 "+3.3V")
  (net 3 "GNDD")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000050")
    (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000051"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000052"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "GND") (zone_connect 2))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V") (zone_connect 2))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000060")
    (at 110 100)
    (property "Reference" "C1" (at 0 -1.5 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000061"))
    (property "Value" "100nF" (at 0 1.5 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000062"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 3 "GNDD") (zone_connect 2))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V") (zone_connect 2))
  )
  (footprint "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000070")
    (at 120 100)
    (property "Reference" "U1" (at 0 -4 0) (layer "F.SilkS")
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (uuid "00000000-0000-0000-0000-000000000071"))
    (property "Value" "NE555" (at 0 4 0) (layer "F.Fab")
      (uuid "00000000-0000-0000-0000-000000000072"))
    (pad "1" smd roundrect (at -2.7 -1.905) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "GND") (zone_connect 2))
    (pad "2" smd roundrect (at -2.7 -0.635) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V") (zone_connect 2))
    (pad "3" smd roundrect (at -2.7 0.635) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 3 "GNDD") (zone_connect 2))
    (pad "4" smd roundrect (at -2.7 1.905) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 1 "GND") (zone_connect 2))
    (pad "5" smd roundrect (at 2.7 1.905) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V"))
    (pad "6" smd roundrect (at 2.7 0.635) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V"))
    (pad "7" smd roundrect (at 2.7 -0.635) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V"))
    (pad "8" smd roundrect (at 2.7 -1.905) (size 1.5 0.6)
      (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25)
      (net 2 "+3.3V"))
  )
  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1)
    (uuid "00000000-0000-0000-0000-000000000080"))
  (segment (start 110 100) (end 120 100) (width 0.25) (layer "F.Cu") (net 2)
    (uuid "00000000-0000-0000-0000-000000000081"))
  (gr_line (start 85 85) (end 145 85) (stroke (width 0.05) (type default)) (layer "Edge.Cuts")
    (uuid "00000000-0000-0000-0000-000000000090"))
  (gr_line (start 145 85) (end 145 115) (stroke (width 0.05) (type default)) (layer "Edge.Cuts")
    (uuid "00000000-0000-0000-0000-000000000091"))
  (gr_line (start 145 115) (end 85 115) (stroke (width 0.05) (type default)) (layer "Edge.Cuts")
    (uuid "00000000-0000-0000-0000-000000000092"))
  (gr_line (start 85 115) (end 85 85) (stroke (width 0.05) (type default)) (layer "Edge.Cuts")
    (uuid "00000000-0000-0000-0000-000000000093"))
  (group "" (uuid "00000000-0000-0000-0000-0000000000a0")
    (members "00000000-0000-0000-0000-000000000050"
             "00000000-0000-0000-0000-000000000060")
  )
)
"""


@pytest.fixture
def pcb_kicad9_no_zones(tmp_path: Path) -> Path:
    """Create a KiCad 9-style PCB file with multiple footprints but no zones."""
    pcb_file = tmp_path / "kicad9_no_zones.kicad_pcb"
    pcb_file.write_text(PCB_KICAD9_NO_ZONES)
    return pcb_file


# ---------------------------------------------------------------------------
# Issue #2064: zone count must be zero when file has no zones
# ---------------------------------------------------------------------------


class TestZoneCountZeroRegression:
    """Regression tests for zone_count accuracy on zone-less boards.

    Addresses: https://github.com/rjwalters/kicad-tools/issues/2064
    """

    def test_zone_count_zero_kicad9_board(self, pcb_kicad9_no_zones: Path):
        """Zone count must be 0 for KiCad 9 board with no zone S-expressions."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        assert pcb.zone_count == 0

    def test_summary_zone_count_zero_kicad9_board(self, pcb_kicad9_no_zones: Path):
        """summary()['zones'] must be 0 for board with no zone S-expressions."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        summary = pcb.summary()
        assert summary["zones"] == 0

    def test_zones_list_empty_kicad9_board(self, pcb_kicad9_no_zones: Path):
        """pcb.zones must be empty for board with no zone S-expressions."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        assert len(pcb.zones) == 0

    def test_zone_connect_pads_not_counted(self, pcb_kicad9_no_zones: Path):
        """Pad-level (zone_connect N) must not inflate zone_count."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        # Board has multiple footprints with zone_connect on pads
        assert pcb.footprint_count >= 3
        # But zone_count must still be 0
        assert pcb.zone_count == 0

    def test_zone_count_matches_sexp_tree(self, pcb_kicad9_no_zones: Path):
        """zone_count must match manual traversal of sexp children."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        manual_count = sum(
            1
            for child in pcb._sexp.children
            if not child.is_atom and child.name == "zone"
        )
        assert pcb.zone_count == manual_count == 0

    def test_pcb_create_has_zero_zones(self):
        """A freshly created PCB must have zero zones."""
        pcb = PCB.create(width=50, height=50, title="test")
        assert pcb.zone_count == 0
        assert pcb.summary()["zones"] == 0

    def test_zone_count_json_output(self, pcb_kicad9_no_zones: Path):
        """CLI JSON output must include zones: 0 for zone-less board."""
        pcb = PCB.load(pcb_kicad9_no_zones)
        summary = pcb.summary()
        output = json.dumps(summary, indent=2)
        data = json.loads(output)
        assert data["zones"] == 0

    def test_zone_count_text_output_shows_zero(self, pcb_kicad9_no_zones: Path, capsys):
        """Text output must display 'Zones: 0' for zone-less board."""
        from types import SimpleNamespace

        from kicad_tools.cli.pcb_query import cmd_summary

        pcb = PCB.load(pcb_kicad9_no_zones)
        args = SimpleNamespace(format="text", pcb=str(pcb_kicad9_no_zones))
        cmd_summary(pcb, args)
        captured = capsys.readouterr()
        assert "Zones: 0" in captured.out
