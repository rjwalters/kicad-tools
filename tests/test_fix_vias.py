"""Tests for the fix-vias command."""

from pathlib import Path

import pytest

from kicad_tools.cli.fix_vias_cmd import find_all_vias, fix_vias, get_design_rules, main
from kicad_tools.sexp.parser import parse_file

# PCB with undersized vias (0.2mm drill, 0.45mm diameter - below JLCPCB minimums)
PCB_WITH_UNDERSIZED_VIAS = """(kicad_pcb
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
  (via (at 110 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
  (via (at 120 110) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 2) (uuid "via-2"))
  (via (at 130 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-3"))
  (segment (start 110 110) (end 120 110) (width 0.25) (layer "F.Cu") (net 1) (uuid "seg-1"))
)
"""


@pytest.fixture
def pcb_with_undersized_vias(tmp_path: Path) -> Path:
    """Create a PCB with undersized vias for testing."""
    pcb_file = tmp_path / "test.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_VIAS)
    return pcb_file


class TestGetDesignRules:
    """Tests for get_design_rules function."""

    def test_explicit_values(self):
        """Explicit values override manufacturer rules."""
        drill, diameter = get_design_rules(None, 2, 1.0, 0.4, 0.8)
        assert drill == 0.4
        assert diameter == 0.8

    def test_partial_override(self):
        """Can override just drill or just diameter."""
        drill, diameter = get_design_rules("jlcpcb", 2, 1.0, 0.4, None)
        assert drill == 0.4
        assert diameter == 0.6  # JLCPCB default

    def test_jlcpcb_defaults(self):
        """JLCPCB 2-layer rules are loaded correctly."""
        drill, diameter = get_design_rules("jlcpcb", 2, 1.0, None, None)
        assert drill == 0.3
        assert diameter == 0.6


class TestFindAllVias:
    """Tests for find_all_vias function."""

    def test_finds_all_vias(self, pcb_with_undersized_vias: Path):
        """Should find all vias in the PCB."""
        doc = parse_file(pcb_with_undersized_vias)
        vias = find_all_vias(doc)

        assert len(vias) == 3

        # Check first via properties
        node, x, y, drill, diameter, net, uuid = vias[0]
        assert x == 110
        assert y == 110
        assert drill == 0.2
        assert diameter == 0.45
        assert net == 1
        assert uuid == "via-1"


class TestFixVias:
    """Tests for fix_vias function."""

    def test_fixes_undersized_vias(self, pcb_with_undersized_vias: Path):
        """Should fix only undersized vias."""
        doc = parse_file(pcb_with_undersized_vias)

        fixes, warnings = fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

        # Should fix 2 vias (via-1 and via-2), not via-3 which is already compliant
        assert len(fixes) == 2

        # Check fix details
        assert fixes[0].old_drill == 0.2
        assert fixes[0].new_drill == 0.3
        assert fixes[0].old_diameter == 0.45
        assert fixes[0].new_diameter == 0.6

    def test_dry_run_does_not_modify(self, pcb_with_undersized_vias: Path):
        """Dry run should not modify the document."""
        doc = parse_file(pcb_with_undersized_vias)

        # Get original values
        vias_before = find_all_vias(doc)
        _, _, _, drill_before, diameter_before, _, _ = vias_before[0]

        # Run fix with dry_run=True
        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=True)

        # Values should be unchanged
        vias_after = find_all_vias(doc)
        _, _, _, drill_after, diameter_after, _, _ = vias_after[0]

        assert drill_before == drill_after
        assert diameter_before == diameter_after

    def test_applies_fixes_when_not_dry_run(self, pcb_with_undersized_vias: Path):
        """Should modify document when not dry run."""
        doc = parse_file(pcb_with_undersized_vias)

        fix_vias(doc, target_drill=0.3, target_diameter=0.6, dry_run=False)

        # Values should be updated
        vias = find_all_vias(doc)
        _, _, _, drill, diameter, _, _ = vias[0]

        assert drill == 0.3
        assert diameter == 0.6

    def test_no_fixes_needed(self, tmp_path: Path):
        """Should return empty list when no fixes needed."""
        # Create PCB with compliant vias
        pcb_content = """(kicad_pcb
          (version 20240108)
          (generator "test")
          (general (thickness 1.6))
          (layers (0 "F.Cu" signal) (31 "B.Cu" signal))
          (setup (pad_to_mask_clearance 0))
          (net 0 "")
          (net 1 "GND")
          (via (at 110 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) (uuid "via-1"))
        )
        """
        pcb_file = tmp_path / "compliant.kicad_pcb"
        pcb_file.write_text(pcb_content)

        doc = parse_file(pcb_file)
        fixes, warnings = fix_vias(doc, target_drill=0.3, target_diameter=0.6)

        assert len(fixes) == 0


class TestCLI:
    """Tests for the CLI interface."""

    def test_dry_run(self, pcb_with_undersized_vias: Path, capsys):
        """Dry run should show changes but not modify file."""
        original_content = pcb_with_undersized_vias.read_text()

        result = main([str(pcb_with_undersized_vias), "--dry-run"])

        # Should succeed
        assert result == 0

        # File should be unchanged
        assert pcb_with_undersized_vias.read_text() == original_content

        # Should show output
        captured = capsys.readouterr()
        assert "via" in captured.out.lower()

    def test_output_to_different_file(self, pcb_with_undersized_vias: Path, tmp_path: Path):
        """Should write to output file when specified."""
        original_content = pcb_with_undersized_vias.read_text()
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_with_undersized_vias), "-o", str(output_file)])

        assert result == 0

        # Original should be unchanged
        assert pcb_with_undersized_vias.read_text() == original_content

        # Output file should exist and contain fixes
        assert output_file.exists()
        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _ = vias[0]
        assert drill == 0.3
        assert diameter == 0.6

    def test_json_output(self, pcb_with_undersized_vias: Path, capsys):
        """JSON output should be valid JSON."""
        import json

        result = main([str(pcb_with_undersized_vias), "--dry-run", "--format", "json"])

        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "fixes" in data
        assert len(data["fixes"]) == 2

    def test_summary_output(self, pcb_with_undersized_vias: Path, capsys):
        """Summary output should show counts."""
        result = main([str(pcb_with_undersized_vias), "--dry-run", "--format", "summary"])

        assert result == 0

        captured = capsys.readouterr()
        assert "2" in captured.out  # 2 vias fixed
        assert "via" in captured.out.lower()

    def test_explicit_sizes(self, pcb_with_undersized_vias: Path, tmp_path: Path):
        """Should use explicit sizes when specified."""
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main(
            [
                str(pcb_with_undersized_vias),
                "--drill",
                "0.35",
                "--diameter",
                "0.7",
                "-o",
                str(output_file),
            ]
        )

        assert result == 0

        output_doc = parse_file(output_file)
        vias = find_all_vias(output_doc)
        _, _, _, drill, diameter, _, _ = vias[0]

        # Should use specified values
        assert drill == 0.35
        assert diameter == 0.7

    def test_invalid_file(self, tmp_path: Path):
        """Should fail gracefully for non-existent file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_quiet_mode(self, pcb_with_undersized_vias: Path, tmp_path: Path, capsys):
        """Quiet mode should suppress output."""
        output_file = tmp_path / "fixed.kicad_pcb"

        result = main([str(pcb_with_undersized_vias), "-o", str(output_file), "--quiet"])

        assert result == 0

        captured = capsys.readouterr()
        assert captured.out == ""
