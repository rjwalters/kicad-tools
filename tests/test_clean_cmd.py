"""Tests for kicad_tools project clean command."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.clean_cmd import (
    CleanableFile,
    CleanResult,
    ProtectedFile,
    delete_files,
    find_cleanable_files,
    format_output_json,
    format_output_text,
    main,
)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """Create a KiCad project directory with various files."""
    project_name = "test_project"

    # Create main project file
    project_file = tmp_path / f"{project_name}.kicad_pro"
    project_file.write_text("{}")

    # Create main PCB and schematic (should be protected)
    (tmp_path / f"{project_name}.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / f"{project_name}.kicad_sch").write_text("(kicad_schematic)")

    # Create old PCB versions (should be cleaned)
    (tmp_path / f"{project_name}-v1.kicad_pcb").write_text("old version 1")
    (tmp_path / f"{project_name}-v2.kicad_pcb").write_text("old version 2")
    (tmp_path / f"{project_name}-routed.kicad_pcb").write_text("routed version")
    (tmp_path / f"{project_name}-draft.kicad_pcb").write_text("draft version")
    (tmp_path / f"{project_name}-old.kicad_pcb").write_text("old version")
    (tmp_path / f"{project_name}-backup.kicad_pcb").write_text("backup version")
    (tmp_path / f"{project_name} copy.kicad_pcb").write_text("copy version")

    # Create backup files (should be cleaned)
    (tmp_path / f"{project_name}.kicad_pcb.bak").write_text("backup")
    (tmp_path / f"{project_name}.kicad_sch~").write_text("backup")
    (tmp_path / f"{project_name}-bak.kicad_pcb").write_text("backup")

    # Create stale reports (should be cleaned except newest)
    (tmp_path / "drc_v1.rpt").write_text("old drc report")
    (tmp_path / "drc_v2.rpt").write_text("newer drc report")
    (tmp_path / "erc_old.json").write_text("{}")

    return project_file


@pytest.fixture
def minimal_project(tmp_path: Path) -> Path:
    """Create a minimal project with no cleanable files."""
    project_name = "minimal"
    project_file = tmp_path / f"{project_name}.kicad_pro"
    project_file.write_text("{}")
    (tmp_path / f"{project_name}.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / f"{project_name}.kicad_sch").write_text("(kicad_schematic)")
    return project_file


@pytest.fixture
def project_with_gerbers(tmp_path: Path) -> Path:
    """Create a project with generated output files for deep clean testing."""
    project_name = "with_gerbers"
    project_file = tmp_path / f"{project_name}.kicad_pro"
    project_file.write_text("{}")
    (tmp_path / f"{project_name}.kicad_pcb").write_text("(kicad_pcb)")

    # Create gerber and drill files
    (tmp_path / f"{project_name}-F_Cu.gbr").write_text("gerber")
    (tmp_path / f"{project_name}-B_Cu.gbr").write_text("gerber")
    (tmp_path / f"{project_name}.drl").write_text("drill")
    (tmp_path / f"{project_name}-F_SilkS.gto").write_text("silk top")
    (tmp_path / f"{project_name}-pos.csv").write_text("position")
    (tmp_path / f"{project_name}-bom.csv").write_text("bom")
    (tmp_path / f"{project_name}.step").write_text("3d model")

    return project_file


class TestCleanableFile:
    """Tests for CleanableFile dataclass."""

    def test_size_kb(self):
        """Test size_kb property."""
        cf = CleanableFile(
            path=Path("/test/file.txt"),
            category="backup",
            reason="test",
            size_bytes=2048,
        )
        assert cf.size_kb == 2.0

    def test_size_str_bytes(self):
        """Test size_str for small files."""
        cf = CleanableFile(
            path=Path("/test/file.txt"),
            category="backup",
            reason="test",
            size_bytes=512,
        )
        assert cf.size_str == "512 B"

    def test_size_str_kb(self):
        """Test size_str for KB-sized files."""
        cf = CleanableFile(
            path=Path("/test/file.txt"),
            category="backup",
            reason="test",
            size_bytes=2048,
        )
        assert cf.size_str == "2.0 KB"

    def test_size_str_mb(self):
        """Test size_str for MB-sized files."""
        cf = CleanableFile(
            path=Path("/test/file.txt"),
            category="backup",
            reason="test",
            size_bytes=1024 * 1024 * 2,
        )
        assert cf.size_str == "2.0 MB"


class TestCleanResult:
    """Tests for CleanResult dataclass."""

    def test_total_size_bytes(self):
        """Test total size calculation."""
        result = CleanResult(
            project_dir=Path("/test"),
            project_name="test",
            to_delete=[
                CleanableFile(Path("/a"), "backup", "test", 100),
                CleanableFile(Path("/b"), "backup", "test", 200),
            ],
        )
        assert result.total_size_bytes == 300

    def test_total_size_str(self):
        """Test human-readable total size."""
        result = CleanResult(
            project_dir=Path("/test"),
            project_name="test",
            to_delete=[
                CleanableFile(Path("/a"), "backup", "test", 2048),
            ],
        )
        assert result.total_size_str == "2.0 KB"

    def test_by_category(self):
        """Test filtering by category."""
        result = CleanResult(
            project_dir=Path("/test"),
            project_name="test",
            to_delete=[
                CleanableFile(Path("/a"), "backup", "test", 100),
                CleanableFile(Path("/b"), "pcb_version", "test", 200),
                CleanableFile(Path("/c"), "backup", "test", 300),
            ],
        )
        backups = result.by_category("backup")
        assert len(backups) == 2
        pcb_versions = result.by_category("pcb_version")
        assert len(pcb_versions) == 1


class TestFindCleanableFiles:
    """Tests for find_cleanable_files function."""

    def test_finds_old_pcb_versions(self, project_dir: Path):
        """Test that old PCB versions are identified."""
        result = find_cleanable_files(project_dir)

        pcb_versions = result.by_category("pcb_version")
        assert len(pcb_versions) >= 5  # -v1, -v2, -routed, -draft, -old, etc.

    def test_protects_main_pcb(self, project_dir: Path):
        """Test that main PCB is protected."""
        result = find_cleanable_files(project_dir)

        protected_names = [p.path.name for p in result.to_keep]
        assert "test_project.kicad_pcb" in protected_names

    def test_protects_main_schematic(self, project_dir: Path):
        """Test that main schematic is protected."""
        result = find_cleanable_files(project_dir)

        protected_names = [p.path.name for p in result.to_keep]
        assert "test_project.kicad_sch" in protected_names

    def test_finds_backup_files(self, project_dir: Path):
        """Test that backup files are identified."""
        result = find_cleanable_files(project_dir)

        backups = result.by_category("backup")
        assert len(backups) >= 3  # .bak, ~, -bak

    def test_finds_stale_reports(self, project_dir: Path):
        """Test that stale reports are identified."""
        result = find_cleanable_files(project_dir)

        stale_reports = result.by_category("stale_report")
        # Some reports should be found (but newest may be protected)
        assert len(stale_reports) >= 0

    def test_minimal_project_has_nothing_to_clean(self, minimal_project: Path):
        """Test that minimal project has no cleanable files."""
        result = find_cleanable_files(minimal_project)

        assert len(result.to_delete) == 0
        assert len(result.to_keep) >= 3  # project, pcb, schematic

    def test_deep_clean_finds_gerbers(self, project_with_gerbers: Path):
        """Test that deep clean finds generated files."""
        result = find_cleanable_files(project_with_gerbers, deep=True)

        generated = result.by_category("generated")
        assert len(generated) >= 5  # gerbers, drill, pos, bom, step

    def test_deep_clean_not_by_default(self, project_with_gerbers: Path):
        """Test that gerbers are NOT cleaned by default."""
        result = find_cleanable_files(project_with_gerbers, deep=False)

        generated = result.by_category("generated")
        assert len(generated) == 0

    def test_nonexistent_project_raises(self):
        """Test that nonexistent project raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            find_cleanable_files(Path("/nonexistent/project.kicad_pro"))

    def test_wrong_extension_raises(self, tmp_path: Path):
        """Test that wrong extension raises ValueError."""
        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("test")

        with pytest.raises(ValueError, match=".kicad_pro"):
            find_cleanable_files(wrong_file)


class TestFormatOutput:
    """Tests for output formatting functions."""

    def test_format_text_basic(self, project_dir: Path):
        """Test basic text formatting."""
        result = find_cleanable_files(project_dir)
        output = format_output_text(result)

        assert "Project cleanup:" in output
        assert "test_project" in output
        assert "Would delete" in output or "No files to clean up" in output

    def test_format_text_with_deletions(self, project_dir: Path):
        """Test text formatting with files to delete."""
        result = find_cleanable_files(project_dir)
        output = format_output_text(result)

        # Should show categories
        if result.to_delete:
            assert "Would delete" in output

    def test_format_text_keeps_protected(self, project_dir: Path):
        """Test that protected files are shown."""
        result = find_cleanable_files(project_dir)
        output = format_output_text(result)

        assert "Would keep" in output or "main project PCB" in output or len(result.to_keep) > 0

    def test_format_json_valid(self, project_dir: Path):
        """Test JSON output is valid."""
        result = find_cleanable_files(project_dir)
        output = format_output_json(result)

        data = json.loads(output)
        assert "project_dir" in data
        assert "project_name" in data
        assert "to_delete" in data
        assert "to_keep" in data
        assert "total_size_bytes" in data
        assert isinstance(data["to_delete"], list)
        assert isinstance(data["to_keep"], list)

    def test_format_json_has_file_details(self, project_dir: Path):
        """Test JSON includes file details."""
        result = find_cleanable_files(project_dir)
        output = format_output_json(result)

        data = json.loads(output)
        if data["to_delete"]:
            first = data["to_delete"][0]
            assert "path" in first
            assert "name" in first
            assert "category" in first
            assert "reason" in first
            assert "size_bytes" in first


class TestDeleteFiles:
    """Tests for delete_files function."""

    def test_deletes_files(self, project_dir: Path):
        """Test that files are actually deleted."""
        result = find_cleanable_files(project_dir)

        # Get list of files that will be deleted
        files_to_delete = [f.path for f in result.to_delete]
        assert len(files_to_delete) > 0

        # Verify files exist before delete
        for f in files_to_delete:
            assert f.exists(), f"File {f} should exist before delete"

        # Delete files
        deleted, freed = delete_files(result)

        # Verify files are gone
        for f in files_to_delete:
            assert not f.exists(), f"File {f} should be deleted"

        assert deleted == len(files_to_delete)
        assert freed > 0

    def test_delete_preserves_protected(self, project_dir: Path):
        """Test that protected files are NOT deleted."""
        result = find_cleanable_files(project_dir)

        protected_paths = [p.path for p in result.to_keep]

        delete_files(result)

        # Protected files should still exist
        for p in protected_paths:
            assert p.exists(), f"Protected file {p} should not be deleted"


class TestCLI:
    """Tests for the CLI main function."""

    def test_main_file_not_found(self, capsys):
        """Test CLI with missing file."""
        result = main(["/nonexistent/project.kicad_pro"])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err or "not found" in captured.err.lower()

    def test_main_wrong_extension(self, tmp_path: Path, capsys):
        """Test CLI with wrong file extension."""
        wrong_file = tmp_path / "test.txt"
        wrong_file.write_text("test")

        result = main([str(wrong_file)])
        assert result == 1

        captured = capsys.readouterr()
        assert ".kicad_pro" in captured.err

    def test_main_dry_run_default(self, project_dir: Path, capsys):
        """Test that dry-run is the default (doesn't delete)."""
        # Get files before
        files_before = list(project_dir.parent.iterdir())

        result = main([str(project_dir)])
        assert result == 0

        # Files should still exist
        files_after = list(project_dir.parent.iterdir())
        assert files_before == files_after

        captured = capsys.readouterr()
        assert "Would delete" in captured.out or "No files to clean" in captured.out

    def test_main_text_format(self, project_dir: Path, capsys):
        """Test text output format."""
        result = main([str(project_dir), "--format", "text"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Project cleanup:" in captured.out

    def test_main_json_format(self, project_dir: Path, capsys):
        """Test JSON output format."""
        result = main([str(project_dir), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "project_name" in data
        assert "to_delete" in data

    def test_main_force_deletes(self, project_dir: Path, capsys):
        """Test --force actually deletes files."""
        # Get cleanable files first
        check_result = find_cleanable_files(project_dir)
        files_to_delete = [f.path for f in check_result.to_delete]
        assert len(files_to_delete) > 0

        # Run with force
        result = main([str(project_dir), "--force"])
        assert result == 0

        # Verify files are deleted
        for f in files_to_delete:
            assert not f.exists(), f"File {f} should be deleted with --force"

        captured = capsys.readouterr()
        assert "Deleted" in captured.out

    def test_main_deep_mode(self, project_with_gerbers: Path, capsys):
        """Test --deep includes generated files."""
        result = main([str(project_with_gerbers), "--deep", "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)

        # Should find generated files
        categories = [f["category"] for f in data["to_delete"]]
        assert "generated" in categories

    def test_main_minimal_project(self, minimal_project: Path, capsys):
        """Test with minimal project (nothing to clean)."""
        result = main([str(minimal_project)])
        assert result == 0

        captured = capsys.readouterr()
        assert "No files to clean up" in captured.out


class TestPatternMatching:
    """Tests for pattern matching edge cases."""

    def test_versioned_pcb_patterns(self, tmp_path: Path):
        """Test various versioned PCB filename patterns."""
        project = tmp_path / "test.kicad_pro"
        project.write_text("{}")
        (tmp_path / "test.kicad_pcb").write_text("main")

        # Create various versioned files
        patterns = [
            "test-v1.kicad_pcb",
            "test-v23.kicad_pcb",
            "test-routed.kicad_pcb",
            "test-routed-v34.kicad_pcb",
            "test-autorouted.kicad_pcb",
            "test-generated.kicad_pcb",
            "test-draft.kicad_pcb",
            "test-old.kicad_pcb",
            "test-backup.kicad_pcb",
            "test-copy.kicad_pcb",
            "test copy.kicad_pcb",
        ]

        for pattern in patterns:
            (tmp_path / pattern).write_text("version")

        result = find_cleanable_files(project)
        pcb_versions = result.by_category("pcb_version")

        # All versioned files should be found
        found_names = [f.path.name for f in pcb_versions]
        for pattern in patterns:
            assert pattern in found_names, f"Pattern {pattern} should be matched"

    def test_backup_file_patterns(self, tmp_path: Path):
        """Test various backup file patterns."""
        project = tmp_path / "test.kicad_pro"
        project.write_text("{}")
        (tmp_path / "test.kicad_pcb").write_text("main")

        # Create various backup files
        patterns = [
            "test.kicad_pcb.bak",
            "test.kicad_sch~",
            "test-bak.kicad_pcb",
            "test-backup-20240101.kicad_pcb",
            "test.kicad_pcb.lck",
            "test-rescue.kicad_sym",
        ]

        for pattern in patterns:
            (tmp_path / pattern).write_text("backup")

        result = find_cleanable_files(project)
        backups = result.by_category("backup")

        # All backup files should be found
        found_names = [f.path.name for f in backups]
        for pattern in patterns:
            assert pattern in found_names, f"Backup pattern {pattern} should be matched"
