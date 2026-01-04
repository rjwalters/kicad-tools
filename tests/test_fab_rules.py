"""Tests for kicad-fab-rules CLI tool."""

import json

import pytest

from kicad_tools.cli.fab_rules_cmd import (
    ProjectRules,
    compare_rules,
    main,
)
from kicad_tools.manufacturers import get_profile

# Minimal KiCad project for testing (values exceed JLCPCB 2-layer requirements)
MINIMAL_PROJECT = {
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": 0.15,  # JLCPCB 2-layer: 0.127mm
                "min_track_width": 0.15,  # JLCPCB 2-layer: 0.127mm
                "min_via_diameter": 0.6,  # JLCPCB 2-layer: 0.5mm
                "min_via_hole": 0.35,  # JLCPCB 2-layer: 0.3mm
                "min_via_annular_width": 0.15,  # JLCPCB 2-layer: 0.1mm
                "min_through_hole_diameter": 0.35,  # JLCPCB 2-layer: 0.3mm
                "min_copper_edge_clearance": 0.35,  # JLCPCB 2-layer: 0.3mm
            }
        }
    },
    "meta": {},
}

# Project with rules that are too loose for manufacturing
LOOSE_PROJECT = {
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": 0.05,  # Way too small
                "min_track_width": 0.05,  # Way too small
                "min_via_diameter": 0.3,
                "min_via_hole": 0.1,  # Too small
                "min_via_annular_width": 0.05,
                "min_through_hole_diameter": 0.2,
                "min_copper_edge_clearance": 0.1,
            }
        }
    },
    "meta": {},
}

# Project with rules stricter than needed
STRICT_PROJECT = {
    "board": {
        "design_settings": {
            "rules": {
                "min_clearance": 0.3,  # Way stricter than needed
                "min_track_width": 0.3,
                "min_via_diameter": 0.8,
                "min_via_hole": 0.5,
                "min_via_annular_width": 0.25,
                "min_through_hole_diameter": 0.5,
                "min_copper_edge_clearance": 0.5,
            }
        }
    },
    "meta": {},
}


class TestProjectRules:
    """Tests for ProjectRules extraction."""

    def test_from_project_with_rules(self):
        """Test extracting rules from a project with design rules."""
        rules = ProjectRules.from_project(MINIMAL_PROJECT)

        assert rules.min_clearance_mm == pytest.approx(0.15)
        assert rules.min_track_width_mm == pytest.approx(0.15)
        assert rules.min_via_diameter_mm == pytest.approx(0.6)
        assert rules.min_via_drill_mm == pytest.approx(0.35)
        assert rules.min_annular_ring_mm == pytest.approx(0.15)
        assert rules.min_hole_diameter_mm == pytest.approx(0.35)
        assert rules.min_copper_to_edge_mm == pytest.approx(0.35)

    def test_from_project_empty(self):
        """Test extracting rules from an empty project."""
        rules = ProjectRules.from_project({})

        assert rules.min_clearance_mm is None
        assert rules.min_track_width_mm is None
        assert rules.min_via_diameter_mm is None

    def test_from_project_partial(self):
        """Test extracting rules from a project with partial rules."""
        partial_project = {
            "board": {
                "design_settings": {
                    "rules": {
                        "min_clearance": 0.15,
                    }
                }
            }
        }
        rules = ProjectRules.from_project(partial_project)

        assert rules.min_clearance_mm == pytest.approx(0.15)
        assert rules.min_track_width_mm is None


class TestCompareRules:
    """Tests for rule comparison logic."""

    def test_compare_compatible_rules(self):
        """Test comparing compatible project rules."""
        project_rules = ProjectRules.from_project(MINIMAL_PROJECT)
        mfr_rules = get_profile("jlcpcb").get_design_rules(layers=2)

        comparisons = compare_rules(project_rules, mfr_rules)

        # All rules should be OK or stricter
        statuses = {c.name: c.status for c in comparisons}
        assert all(s in ("ok", "stricter") for s in statuses.values())

    def test_compare_loose_rules(self):
        """Test comparing rules that are too loose."""
        project_rules = ProjectRules.from_project(LOOSE_PROJECT)
        mfr_rules = get_profile("jlcpcb").get_design_rules(layers=2)

        comparisons = compare_rules(project_rules, mfr_rules)

        # Should have loose rules
        statuses = {c.name: c.status for c in comparisons}
        assert "loose" in statuses.values()

        # Specifically check clearance is loose
        clearance = next(c for c in comparisons if c.name == "Min clearance")
        assert clearance.status == "loose"
        assert clearance.recommendation is not None

    def test_compare_strict_rules(self):
        """Test comparing rules that are stricter than needed."""
        project_rules = ProjectRules.from_project(STRICT_PROJECT)
        mfr_rules = get_profile("jlcpcb").get_design_rules(layers=2)

        comparisons = compare_rules(project_rules, mfr_rules)

        # Should have stricter-than-needed rules
        statuses = {c.name: c.status for c in comparisons}
        assert "stricter" in statuses.values()

    def test_compare_missing_rules(self):
        """Test comparing when project has missing rules."""
        project_rules = ProjectRules.from_project({})
        mfr_rules = get_profile("jlcpcb").get_design_rules(layers=2)

        comparisons = compare_rules(project_rules, mfr_rules)

        # All rules should be missing
        statuses = {c.name: c.status for c in comparisons}
        assert all(s == "missing" for s in statuses.values())


class TestCmdList:
    """Tests for the list command."""

    def test_list_table_format(self, capsys):
        """Test list command with table format."""
        exit_code = main(["list"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "jlcpcb" in captured.out
        assert "oshpark" in captured.out
        assert "pcbway" in captured.out
        assert "seeed" in captured.out

    def test_list_json_format(self, capsys):
        """Test list command with JSON format."""
        exit_code = main(["list", "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 4

        ids = {p["id"] for p in data}
        assert "jlcpcb" in ids
        assert "oshpark" in ids


class TestCmdShow:
    """Tests for the show command."""

    def test_show_jlcpcb(self, capsys):
        """Test showing JLCPCB profile."""
        exit_code = main(["show", "jlcpcb"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "JLCPCB" in captured.out
        assert "Min trace width" in captured.out

    def test_show_with_layers(self, capsys):
        """Test showing profile with specific layer count."""
        exit_code = main(["show", "jlcpcb", "--layers", "4"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "4-layer" in captured.out

    def test_show_json_format(self, capsys):
        """Test showing profile in JSON format."""
        exit_code = main(["show", "jlcpcb", "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "profile" in data
        assert "design_rules" in data
        assert data["profile"]["id"] == "jlcpcb"

    def test_show_invalid_profile(self, capsys):
        """Test showing invalid profile."""
        exit_code = main(["show", "invalid"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err


class TestCmdCompare:
    """Tests for the compare command."""

    def test_compare_compatible(self, capsys, tmp_path):
        """Test comparing a compatible project."""
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text(json.dumps(MINIMAL_PROJECT))

        exit_code = main(["compare", "jlcpcb", str(project_file)])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "COMPATIBLE" in captured.out

    def test_compare_incompatible(self, capsys, tmp_path):
        """Test comparing an incompatible project."""
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text(json.dumps(LOOSE_PROJECT))

        exit_code = main(["compare", "jlcpcb", str(project_file)])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "INCOMPATIBLE" in captured.out
        assert "Too loose" in captured.out

    def test_compare_json_format(self, capsys, tmp_path):
        """Test comparing with JSON output."""
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text(json.dumps(MINIMAL_PROJECT))

        exit_code = main(["compare", "jlcpcb", str(project_file), "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "comparisons" in data
        assert "compatible" in data
        assert data["compatible"] is True

    def test_compare_file_not_found(self, capsys):
        """Test comparing with non-existent file."""
        exit_code = main(["compare", "jlcpcb", "/nonexistent/file.kicad_pro"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_compare_wrong_extension(self, capsys, tmp_path):
        """Test comparing with wrong file extension."""
        wrong_file = tmp_path / "test.kicad_pcb"
        wrong_file.write_text("{}")

        exit_code = main(["compare", "jlcpcb", str(wrong_file)])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Expected .kicad_pro" in captured.err


class TestCmdApply:
    """Tests for the apply command."""

    def test_apply_rules(self, capsys, tmp_path):
        """Test applying manufacturer rules to a project."""
        project_file = tmp_path / "test.kicad_pro"
        project_file.write_text(json.dumps({"meta": {}}))

        exit_code = main(["apply", "jlcpcb", str(project_file)])
        assert exit_code == 0

        # Verify the file was updated
        updated = json.loads(project_file.read_text())
        assert "board" in updated
        assert updated["meta"]["manufacturer"] == "jlcpcb"

    def test_apply_dry_run(self, capsys, tmp_path):
        """Test applying rules with dry-run flag."""
        project_file = tmp_path / "test.kicad_pro"
        original_content = json.dumps({"meta": {}})
        project_file.write_text(original_content)

        exit_code = main(["apply", "jlcpcb", str(project_file), "--dry-run"])
        assert exit_code == 0

        # Verify file was not modified
        assert project_file.read_text() == original_content

        captured = capsys.readouterr()
        assert "dry run" in captured.out

    def test_apply_with_output(self, capsys, tmp_path):
        """Test applying rules to a different output file."""
        project_file = tmp_path / "test.kicad_pro"
        output_file = tmp_path / "output.kicad_pro"
        project_file.write_text(json.dumps({"meta": {}}))

        exit_code = main(["apply", "jlcpcb", str(project_file), "-o", str(output_file)])
        assert exit_code == 0

        # Verify output file was created
        assert output_file.exists()
        updated = json.loads(output_file.read_text())
        assert updated["meta"]["manufacturer"] == "jlcpcb"


class TestCmdExport:
    """Tests for the export command."""

    def test_export_json(self, capsys):
        """Test exporting rules as JSON."""
        exit_code = main(["export", "jlcpcb", "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["manufacturer"] == "jlcpcb"
        assert "rules" in data
        assert "min_trace_width_mm" in data["rules"]

    def test_export_dru(self, capsys):
        """Test exporting rules as KiCad DRU format."""
        exit_code = main(["export", "jlcpcb", "--format", "kicad_dru"])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "(version 1)" in captured.out
        assert "(rule" in captured.out
        assert "JLCPCB" in captured.out

    def test_export_to_file(self, capsys, tmp_path):
        """Test exporting rules to a file."""
        output_file = tmp_path / "rules.json"

        exit_code = main(["export", "jlcpcb", "-o", str(output_file)])
        assert exit_code == 0

        assert output_file.exists()
        data = json.loads(output_file.read_text())
        assert data["manufacturer"] == "jlcpcb"

    def test_export_with_layers(self, capsys):
        """Test exporting rules for specific layer count."""
        exit_code = main(["export", "jlcpcb", "--layers", "6", "--format", "json"])
        assert exit_code == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["layers"] == 6


class TestMainHelp:
    """Tests for help and argument handling."""

    def test_no_command_shows_help(self, capsys):
        """Test that no command shows help."""
        exit_code = main([])
        assert exit_code == 0

        captured = capsys.readouterr()
        assert "kicad-fab-rules" in captured.out
        assert "list" in captured.out
        assert "show" in captured.out
        assert "compare" in captured.out

    def test_invalid_profile(self, capsys):
        """Test invalid manufacturer profile."""
        exit_code = main(["show", "invalid_mfr"])
        assert exit_code == 1

        captured = capsys.readouterr()
        assert "Unknown manufacturer" in captured.err
