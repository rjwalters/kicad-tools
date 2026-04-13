"""Tests for the fix-silkscreen command and SilkscreenRepairer."""

from pathlib import Path

import pytest

from kicad_tools.cli.fix_silkscreen_cmd import _get_min_width, main
from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer
from kicad_tools.sexp.parser import parse_file

# --------------------------------------------------------------------------
# Fixtures: synthetic PCB content
# --------------------------------------------------------------------------

# PCB with undersized silkscreen lines in footprints (0.12mm < 0.15mm JLCPCB min).
PCB_WITH_UNDERSIZED_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1")
    (fp_line (start -0.153641 -0.38) (end 0.153641 -0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
    (fp_line (start -0.153641 0.38) (end 0.153641 0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
    (fp_rect (start -0.5 -0.3) (end 0.5 0.3)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 110 100)
    (property "Reference" "C1")
    (fp_line (start -0.153641 -0.38) (end 0.153641 -0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  )
)
"""

# PCB with a board-level silkscreen graphic that is undersized.
PCB_WITH_BOARD_LEVEL_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_line (start 10 10) (end 50 10)
    (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  (gr_rect (start 10 20) (end 50 30)
    (stroke (width 0.08) (type solid)) (layer "B.SilkS"))
)
"""

# PCB with non-silkscreen graphics (should NOT be modified).
PCB_WITH_NON_SILK_GRAPHICS = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Connector:PinHeader_1x02"
    (layer "F.Cu")
    (at 50 50)
    (property "Reference" "J1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.Cu"))
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
  (gr_line (start 0 0) (end 100 0)
    (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
)
"""

# PCB with a zero-width stroke (should NOT be widened).
PCB_WITH_ZERO_WIDTH = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:ZeroWidth"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0) (type solid)) (layer "F.SilkS"))
  )
)
"""

# PCB with lines already meeting minimum width.
PCB_WITH_OK_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:OkWidth"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.15) (type solid)) (layer "F.SilkS"))
    (fp_line (start 0 0) (end 0 5)
      (stroke (width 0.20) (type solid)) (layer "F.SilkS"))
  )
)
"""


# --------------------------------------------------------------------------
# Pytest fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def pcb_undersized(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "undersized.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_SILK)
    return pcb_file


@pytest.fixture
def pcb_board_level(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "board_level.kicad_pcb"
    pcb_file.write_text(PCB_WITH_BOARD_LEVEL_SILK)
    return pcb_file


@pytest.fixture
def pcb_non_silk(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "non_silk.kicad_pcb"
    pcb_file.write_text(PCB_WITH_NON_SILK_GRAPHICS)
    return pcb_file


@pytest.fixture
def pcb_zero_width(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "zero_width.kicad_pcb"
    pcb_file.write_text(PCB_WITH_ZERO_WIDTH)
    return pcb_file


@pytest.fixture
def pcb_ok(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "ok.kicad_pcb"
    pcb_file.write_text(PCB_WITH_OK_SILK)
    return pcb_file


# --------------------------------------------------------------------------
# Tests: _get_min_width
# --------------------------------------------------------------------------


class TestGetMinWidth:
    """Tests for the _get_min_width helper."""

    def test_explicit_width_overrides(self):
        """Explicit --min-width takes precedence over manufacturer."""
        assert _get_min_width("jlcpcb", 2, 1.0, 0.20) == 0.20

    def test_jlcpcb_default(self):
        """JLCPCB 2-layer 1oz returns 0.15mm."""
        assert _get_min_width("jlcpcb", 2, 1.0, None) == 0.15

    def test_no_mfr_fallback(self):
        """No manufacturer returns sensible default."""
        assert _get_min_width(None, 2, 1.0, None) == 0.15


# --------------------------------------------------------------------------
# Tests: SilkscreenRepairer
# --------------------------------------------------------------------------


class TestSilkscreenRepairer:
    """Tests for the core repair logic."""

    def test_fixes_undersized_fp_lines(self, pcb_undersized: Path):
        """Undersized fp_line and fp_rect strokes are widened."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        # R1 has 2 fp_line (0.12) + 1 fp_rect (0.10), C1 has 1 fp_line (0.12) = 4 total
        assert result.total_fixed == 4

        # Verify all fixes are for silkscreen elements
        for fix in result.fixes:
            assert fix.new_width == 0.15
            assert fix.old_width < 0.15

    def test_dry_run_no_mutation(self, pcb_undersized: Path):
        """dry_run=True collects fixes but does not mutate the tree."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15, dry_run=True)

        assert result.total_fixed == 4

        # Save and verify original is unchanged
        repairer.save()
        # After save with dry_run, the SExp tree should still have the old widths.
        # Re-parse to check.
        doc = parse_file(pcb_undersized)
        # Find the first fp_line in the first footprint
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        # Should still be 0.12 because dry_run prevented mutation
        assert float(width.get_first_atom()) == 0.12

    def test_mutates_tree_when_not_dry_run(self, pcb_undersized: Path):
        """Without dry_run, the SExp tree is actually modified."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15, dry_run=False)

        assert result.total_fixed == 4

        # Verify the tree was mutated
        fp = repairer.doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_board_level_graphics(self, pcb_board_level: Path):
        """Board-level gr_line and gr_rect on silk layers are widened."""
        repairer = SilkscreenRepairer(pcb_board_level)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 2
        assert result.fixes[0].element_type == "gr_line"
        assert result.fixes[0].footprint_ref == ""
        assert result.fixes[1].element_type == "gr_rect"

    def test_non_silk_layers_untouched(self, pcb_non_silk: Path):
        """Graphics on non-silkscreen layers are not modified."""
        repairer = SilkscreenRepairer(pcb_non_silk)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        # Only the fp_line on F.SilkS should be fixed, not F.Cu or Edge.Cuts
        assert result.total_fixed == 1
        assert result.fixes[0].layer == "F.SilkS"

    def test_zero_width_not_widened(self, pcb_zero_width: Path):
        """Zero-width strokes (inherit from style) are skipped."""
        repairer = SilkscreenRepairer(pcb_zero_width)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 0

    def test_already_ok_no_fixes(self, pcb_ok: Path):
        """Lines already meeting minimum width are not modified."""
        repairer = SilkscreenRepairer(pcb_ok)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 0

    def test_save_to_output_path(self, pcb_undersized: Path, tmp_path: Path):
        """Saving to a different output path leaves original unchanged."""
        output_path = tmp_path / "output.kicad_pcb"
        original_content = pcb_undersized.read_text()

        repairer = SilkscreenRepairer(pcb_undersized)
        repairer.repair_line_widths(min_width_mm=0.15)
        repairer.save(output_path)

        # Original file should be unchanged (we read from it, save elsewhere)
        assert pcb_undersized.read_text() == original_content

        # Output file should exist and have the fixes
        assert output_path.exists()
        doc = parse_file(output_path)
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_footprint_reference_extraction(self, pcb_undersized: Path):
        """Fix records include the correct footprint reference."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        refs = {f.footprint_ref for f in result.fixes}
        assert "R1" in refs
        assert "C1" in refs


# --------------------------------------------------------------------------
# Tests: CLI main()
# --------------------------------------------------------------------------


class TestCLI:
    """Tests for the fix-silkscreen CLI entry point."""

    def test_dry_run_no_file_change(self, pcb_undersized: Path):
        """--dry-run prints summary and exits 0 without modifying the file."""
        original = pcb_undersized.read_text()
        exit_code = main([str(pcb_undersized), "--dry-run"])
        assert exit_code == 0
        assert pcb_undersized.read_text() == original

    def test_apply_fixes(self, pcb_undersized: Path):
        """Running without --dry-run modifies the file."""
        exit_code = main([str(pcb_undersized)])
        assert exit_code == 0

        # Verify the file was modified
        doc = parse_file(pcb_undersized)
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_output_flag(self, pcb_undersized: Path, tmp_path: Path):
        """--output writes to a different file, leaving original intact."""
        output = tmp_path / "fixed.kicad_pcb"
        original = pcb_undersized.read_text()
        exit_code = main([str(pcb_undersized), "-o", str(output)])
        assert exit_code == 0
        assert pcb_undersized.read_text() == original
        assert output.exists()

    def test_json_output(self, pcb_undersized: Path, capsys):
        """--format json produces valid JSON output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--format", "json"])
        assert exit_code == 0
        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_fixed"] == 4
        assert data["dry_run"] is True

    def test_summary_output(self, pcb_undersized: Path, capsys):
        """--format summary produces compact output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--format", "summary"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "4" in captured.out
        assert "Would fix" in captured.out

    def test_missing_file_error(self, tmp_path: Path):
        """Non-existent PCB file returns exit code 1."""
        exit_code = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert exit_code == 1

    def test_wrong_extension_error(self, tmp_path: Path):
        """Non-.kicad_pcb file returns exit code 1."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("hello")
        exit_code = main([str(bad_file)])
        assert exit_code == 1

    def test_no_violations_returns_zero(self, pcb_ok: Path):
        """When no violations exist, exit code is still 0."""
        exit_code = main([str(pcb_ok)])
        assert exit_code == 0

    def test_min_width_override(self, pcb_undersized: Path):
        """--min-width overrides manufacturer defaults."""
        exit_code = main([str(pcb_undersized), "--min-width", "0.20", "--dry-run"])
        assert exit_code == 0

    def test_quiet_flag(self, pcb_undersized: Path, capsys):
        """--quiet suppresses output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--quiet"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out == ""
