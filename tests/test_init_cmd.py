"""Tests for kicad_tools project init command."""

import json
from pathlib import Path

from kicad_tools.cli.init_cmd import (
    create_dru_content,
    create_gitignore,
    create_gitignore_content,
    create_project_file,
    init_project,
    main,
)
from kicad_tools.manufacturers import get_profile


class TestCreateProjectFile:
    """Tests for create_project_file function."""

    def test_creates_basic_structure(self):
        """Test that basic project structure is created."""
        project_path = Path("/test/myproject.kicad_pro")
        data = create_project_file(project_path, "myproject")

        assert "meta" in data
        assert "board" in data
        assert "schematic" in data
        assert data["meta"]["filename"] == "myproject"

    def test_has_design_settings(self):
        """Test that design_settings structure is created."""
        project_path = Path("/test/myproject.kicad_pro")
        data = create_project_file(project_path, "myproject")

        assert "design_settings" in data["board"]
        assert "rules" in data["board"]["design_settings"]
        assert "defaults" in data["board"]["design_settings"]


class TestCreateDruContent:
    """Tests for create_dru_content function."""

    def test_creates_valid_sexp(self):
        """Test that valid S-expression content is created."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(2)

        content = create_dru_content(rules, "jlcpcb", 2)

        assert content.startswith("(version 1)")
        assert "rule" in content
        assert "constraint" in content

    def test_includes_trace_width_rule(self):
        """Test that trace width rule is included."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(2)

        content = create_dru_content(rules, "jlcpcb", 2)

        assert "Minimum Trace Width" in content
        assert "track_width" in content
        assert f"{rules.min_trace_width_mm}mm" in content

    def test_includes_clearance_rule(self):
        """Test that clearance rule is included."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(2)

        content = create_dru_content(rules, "jlcpcb", 2)

        assert "Minimum Clearance" in content
        assert "clearance" in content

    def test_includes_via_rules(self):
        """Test that via rules are included."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(2)

        content = create_dru_content(rules, "jlcpcb", 2)

        assert "Minimum Via Drill" in content
        assert "Minimum Via Diameter" in content
        assert "hole_size" in content
        assert "via_diameter" in content

    def test_includes_manufacturer_comment(self):
        """Test that manufacturer info is in comments."""
        profile = get_profile("seeed")
        rules = profile.get_design_rules(4)

        content = create_dru_content(rules, "seeed", 4)

        assert "SEEED" in content
        assert "4-layer" in content


class TestInitProject:
    """Tests for init_project function."""

    def test_creates_project_file(self, tmp_path: Path):
        """Test that project file is created."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        assert result == 0
        assert (tmp_path / "testproject.kicad_pro").exists()

    def test_creates_dru_file(self, tmp_path: Path):
        """Test that DRU file is created."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        assert result == 0
        assert (tmp_path / "testproject.kicad_dru").exists()

    def test_project_has_manufacturer_metadata(self, tmp_path: Path):
        """Test that manufacturer metadata is stored."""
        init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=4,
            copper=2.0,
        )

        project_path = tmp_path / "testproject.kicad_pro"
        data = json.loads(project_path.read_text())

        assert data["meta"]["manufacturer"] == "jlcpcb"
        assert data["meta"]["layers"] == 4
        assert data["meta"]["copper_oz"] == 2.0

    def test_project_has_design_rules(self, tmp_path: Path):
        """Test that design rules are applied."""
        init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        project_path = tmp_path / "testproject.kicad_pro"
        data = json.loads(project_path.read_text())

        rules = data["board"]["design_settings"]["rules"]
        assert "min_clearance" in rules
        assert "min_track_width" in rules
        assert "min_via_diameter" in rules
        assert "min_via_hole" in rules

    def test_dry_run_creates_nothing(self, tmp_path: Path):
        """Test that dry-run doesn't create files."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
            dry_run=True,
        )

        assert result == 0
        assert not (tmp_path / "testproject.kicad_pro").exists()
        assert not (tmp_path / "testproject.kicad_dru").exists()

    def test_updates_existing_project(self, tmp_path: Path):
        """Test that existing project is updated."""
        # Create initial project
        project_path = tmp_path / "existing.kicad_pro"
        project_path.write_text(json.dumps({"meta": {"existing": "data"}}))

        result = init_project(
            target=str(project_path),
            manufacturer="seeed",
            layers=4,
        )

        assert result == 0
        data = json.loads(project_path.read_text())
        assert data["meta"]["manufacturer"] == "seeed"
        assert data["meta"]["existing"] == "data"

    def test_invalid_manufacturer_fails(self, tmp_path: Path, capsys):
        """Test that invalid manufacturer returns error."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="invalid_mfr",
            layers=2,
        )

        assert result == 1

    def test_json_output_format(self, tmp_path: Path, capsys):
        """Test JSON output format."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
            dry_run=True,
            output_format="json",
        )

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "project_path" in data
        assert "dru_path" in data
        assert "manufacturer" in data
        assert "rules" in data
        assert data["manufacturer"] == "jlcpcb"

    def test_current_directory_initialization(self, tmp_path: Path, monkeypatch):
        """Test initialization with '.' uses current directory name."""
        monkeypatch.chdir(tmp_path)

        result = init_project(
            target=".",
            manufacturer="jlcpcb",
            layers=2,
        )

        assert result == 0
        # Project file should be named after the tmp directory
        project_files = list(tmp_path.glob("*.kicad_pro"))
        assert len(project_files) == 1

    def test_different_layer_counts(self, tmp_path: Path):
        """Test with different layer counts."""
        for layers in [2, 4, 6]:
            project_name = f"test_{layers}layer"
            result = init_project(
                target=str(tmp_path / project_name),
                manufacturer="jlcpcb",
                layers=layers,
            )

            assert result == 0
            data = json.loads((tmp_path / f"{project_name}.kicad_pro").read_text())
            assert data["meta"]["layers"] == layers

    def test_different_copper_weights(self, tmp_path: Path):
        """Test with different copper weights."""
        for copper in [1.0, 2.0]:
            # Use integer name to avoid dot being interpreted as extension
            project_name = f"test_{int(copper)}oz"
            result = init_project(
                target=str(tmp_path / project_name),
                manufacturer="jlcpcb",
                layers=2,
                copper=copper,
            )

            assert result == 0
            data = json.loads((tmp_path / f"{project_name}.kicad_pro").read_text())
            assert data["meta"]["copper_oz"] == copper


class TestCLI:
    """Tests for the CLI main function."""

    def test_main_basic(self, tmp_path: Path):
        """Test basic CLI invocation."""
        result = main([str(tmp_path / "testproject"), "--mfr", "jlcpcb"])
        assert result == 0
        assert (tmp_path / "testproject.kicad_pro").exists()
        assert (tmp_path / "testproject.kicad_dru").exists()

    def test_main_with_layers(self, tmp_path: Path):
        """Test CLI with --layers option."""
        result = main(
            [
                str(tmp_path / "testproject"),
                "--mfr",
                "jlcpcb",
                "--layers",
                "4",
            ]
        )

        assert result == 0
        data = json.loads((tmp_path / "testproject.kicad_pro").read_text())
        assert data["meta"]["layers"] == 4

    def test_main_with_copper(self, tmp_path: Path):
        """Test CLI with --copper option."""
        result = main(
            [
                str(tmp_path / "testproject"),
                "--mfr",
                "jlcpcb",
                "--copper",
                "2.0",
            ]
        )

        assert result == 0
        data = json.loads((tmp_path / "testproject.kicad_pro").read_text())
        assert data["meta"]["copper_oz"] == 2.0

    def test_main_dry_run(self, tmp_path: Path):
        """Test CLI with --dry-run option."""
        result = main(
            [
                str(tmp_path / "testproject"),
                "--mfr",
                "jlcpcb",
                "--dry-run",
            ]
        )

        assert result == 0
        assert not (tmp_path / "testproject.kicad_pro").exists()

    def test_main_json_format(self, tmp_path: Path, capsys):
        """Test CLI with --format json option."""
        result = main(
            [
                str(tmp_path / "testproject"),
                "--mfr",
                "jlcpcb",
                "--format",
                "json",
                "--dry-run",
            ]
        )

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "rules" in data

    def test_main_invalid_manufacturer(self, tmp_path: Path):
        """Test CLI with invalid manufacturer."""
        result = main(
            [
                str(tmp_path / "testproject"),
                "--mfr",
                "nonexistent",
            ]
        )

        assert result == 1

    def test_main_existing_project(self, tmp_path: Path):
        """Test CLI with existing project file."""
        project_path = tmp_path / "existing.kicad_pro"
        project_path.write_text("{}")

        result = main([str(project_path), "--mfr", "seeed"])

        assert result == 0
        data = json.loads(project_path.read_text())
        assert data["meta"]["manufacturer"] == "seeed"

    def test_main_all_manufacturers(self, tmp_path: Path):
        """Test CLI with all supported manufacturers."""
        manufacturers = ["jlcpcb", "seeed", "pcbway", "oshpark"]

        for mfr in manufacturers:
            project_name = f"test_{mfr}"
            result = main(
                [
                    str(tmp_path / project_name),
                    "--mfr",
                    mfr,
                ]
            )

            assert result == 0, f"Failed for manufacturer: {mfr}"
            assert (tmp_path / f"{project_name}.kicad_pro").exists()
            assert (tmp_path / f"{project_name}.kicad_dru").exists()


class TestDruFileContent:
    """Tests for the generated DRU file content."""

    def test_dru_is_valid_sexp(self, tmp_path: Path):
        """Test that generated DRU file has valid S-expression syntax."""
        init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        dru_content = (tmp_path / "testproject.kicad_dru").read_text()

        # Basic S-expression validation
        assert dru_content.startswith("(version 1)")
        assert dru_content.count("(") == dru_content.count(")")

    def test_dru_has_required_rules(self, tmp_path: Path):
        """Test that DRU file has all required rules."""
        init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        dru_content = (tmp_path / "testproject.kicad_dru").read_text()

        required_rules = [
            "Trace Width",
            "Clearance",
            "Via Drill",
            "Via Diameter",
            "Annular Ring",
            "Copper to Edge",
        ]

        for rule in required_rules:
            assert rule in dru_content, f"Missing rule: {rule}"

    def test_dru_values_match_manufacturer(self, tmp_path: Path):
        """Test that DRU values match manufacturer profile."""
        profile = get_profile("jlcpcb")
        rules = profile.get_design_rules(2)

        init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        dru_content = (tmp_path / "testproject.kicad_dru").read_text()

        # Check that actual values are in the file
        assert f"{rules.min_trace_width_mm}mm" in dru_content
        assert f"{rules.min_clearance_mm}mm" in dru_content
        assert f"{rules.min_via_drill_mm}mm" in dru_content


class TestGitignore:
    """Tests for .gitignore creation in init command."""

    def test_gitignore_content_has_required_patterns(self):
        """Test that gitignore template contains the required patterns."""
        content = create_gitignore_content()

        assert "*.kicad_prl" in content
        assert "drc_report.json" in content
        assert "manufacturing/" in content

    def test_gitignore_content_has_comments(self):
        """Test that gitignore template includes explanatory comments."""
        content = create_gitignore_content()

        assert "# KiCad local settings" in content
        assert "# Build outputs" in content

    def test_create_gitignore_creates_file(self, tmp_path: Path):
        """Test that create_gitignore creates a .gitignore file."""
        result = create_gitignore(tmp_path)

        assert result is True
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "*.kicad_prl" in content

    def test_create_gitignore_skips_existing(self, tmp_path: Path):
        """Test that create_gitignore does not overwrite an existing file."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# existing content\n")

        result = create_gitignore(tmp_path)

        assert result is False
        assert gitignore.read_text() == "# existing content\n"

    def test_create_gitignore_dry_run(self, tmp_path: Path):
        """Test that create_gitignore respects dry_run flag."""
        result = create_gitignore(tmp_path, dry_run=True)

        assert result is None
        assert not (tmp_path / ".gitignore").exists()

    def test_init_project_creates_gitignore(self, tmp_path: Path):
        """Test that init_project creates a .gitignore alongside project files."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        assert result == 0
        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        content = gitignore.read_text()
        assert "*.kicad_prl" in content
        assert "drc_report.json" in content
        assert "manufacturing/" in content

    def test_init_project_does_not_overwrite_gitignore(self, tmp_path: Path):
        """Test that init_project skips .gitignore if one already exists."""
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("# my custom gitignore\n")

        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
        )

        assert result == 0
        assert gitignore.read_text() == "# my custom gitignore\n"

    def test_init_project_dry_run_no_gitignore(self, tmp_path: Path):
        """Test that dry-run does not create .gitignore."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
            dry_run=True,
        )

        assert result == 0
        assert not (tmp_path / ".gitignore").exists()

    def test_json_output_includes_gitignore_fields(self, tmp_path: Path, capsys):
        """Test that JSON output includes gitignore_path and will_create_gitignore."""
        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
            dry_run=True,
            output_format="json",
        )

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert "gitignore_path" in data
        assert "will_create_gitignore" in data
        assert data["will_create_gitignore"] is True
        assert data["gitignore_path"].endswith(".gitignore")

    def test_json_output_will_create_gitignore_false(self, tmp_path: Path, capsys):
        """Test JSON output when .gitignore already exists."""
        (tmp_path / ".gitignore").write_text("# existing\n")

        result = init_project(
            target=str(tmp_path / "testproject"),
            manufacturer="jlcpcb",
            layers=2,
            dry_run=True,
            output_format="json",
        )

        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)

        assert data["will_create_gitignore"] is False
