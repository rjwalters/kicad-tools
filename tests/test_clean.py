"""Tests for project cleanup command (kct clean)."""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.clean_cmd import (
    CleanableFile,
    CleanResult,
    find_cleanable_files,
    format_output_json,
    format_output_text,
)
from kicad_tools.cli.clean_cmd import (
    main as clean_main,
)

# Minimal KiCad project file content
MINIMAL_PROJECT = """{
  "meta": {
    "filename": "test_project.kicad_pro",
    "version": 1
  }
}
"""


@pytest.fixture
def temp_project(tmp_path: Path) -> Path:
    """Create a minimal project structure for testing."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create project file
    project_file = project_dir / "test_project.kicad_pro"
    project_file.write_text(MINIMAL_PROJECT)

    # Create main PCB file
    main_pcb = project_dir / "test_project.kicad_pcb"
    main_pcb.write_text("(kicad_pcb)")

    # Create main schematic file
    main_sch = project_dir / "test_project.kicad_sch"
    main_sch.write_text("(kicad_sch)")

    return project_file


@pytest.fixture
def project_with_old_versions(temp_project: Path) -> Path:
    """Create a project with old PCB versions."""
    project_dir = temp_project.parent

    # Create old PCB versions
    (project_dir / "test_project-v1.kicad_pcb").write_text("(old version 1)")
    (project_dir / "test_project-v2.kicad_pcb").write_text("(old version 2)")
    (project_dir / "test_project-routed.kicad_pcb").write_text("(routed version)")
    (project_dir / "test_project-autorouted.kicad_pcb").write_text("(autorouted)")

    return temp_project


@pytest.fixture
def project_with_stale_reports(temp_project: Path) -> Path:
    """Create a project with stale DRC/ERC reports."""
    project_dir = temp_project.parent

    # Create stale reports
    (project_dir / "drc-v1.txt").write_text("old drc report 1")
    (project_dir / "drc-v2.txt").write_text("old drc report 2")
    (project_dir / "erc-v1.rpt").write_text("old erc report")
    (project_dir / "drc-old.txt").write_text("backup drc report")

    return temp_project


@pytest.fixture
def project_with_backups(temp_project: Path) -> Path:
    """Create a project with backup files."""
    project_dir = temp_project.parent

    # Create backup files
    (project_dir / "test_project.kicad_sch.bak").write_text("backup")
    (project_dir / "test_project.kicad_pcb~").write_text("tilde backup")
    (project_dir / "test_project-bak.kicad_pcb").write_text("kicad backup")
    (project_dir / "test_project.kicad_pcb.lck").write_text("lock file")

    return temp_project


class TestCleanableFile:
    """Tests for CleanableFile dataclass."""

    def test_size_str_bytes(self):
        """Test size string for small files."""
        f = CleanableFile(Path("test.txt"), "backup", "test", size_bytes=500)
        assert f.size_str == "500 B"

    def test_size_str_kilobytes(self):
        """Test size string for KB files."""
        f = CleanableFile(Path("test.txt"), "backup", "test", size_bytes=2048)
        assert f.size_str == "2.0 KB"

    def test_size_str_megabytes(self):
        """Test size string for MB files."""
        f = CleanableFile(Path("test.txt"), "backup", "test", size_bytes=2 * 1024 * 1024)
        assert f.size_str == "2.0 MB"


class TestCleanResult:
    """Tests for CleanResult dataclass."""

    def test_total_size_bytes(self):
        """Test total size calculation."""
        result = CleanResult(
            project_dir=Path("/test"),
            project_name="test",
            to_delete=[
                CleanableFile(Path("a.txt"), "backup", "test", size_bytes=100),
                CleanableFile(Path("b.txt"), "backup", "test", size_bytes=200),
            ],
        )
        assert result.total_size_bytes == 300

    def test_by_category(self):
        """Test filtering by category."""
        result = CleanResult(
            project_dir=Path("/test"),
            project_name="test",
            to_delete=[
                CleanableFile(Path("a.pcb"), "pcb_version", "test", size_bytes=100),
                CleanableFile(Path("b.bak"), "backup", "test", size_bytes=200),
                CleanableFile(Path("c.pcb"), "pcb_version", "test", size_bytes=300),
            ],
        )
        pcb_versions = result.by_category("pcb_version")
        assert len(pcb_versions) == 2
        assert all(f.category == "pcb_version" for f in pcb_versions)


class TestFindCleanableFiles:
    """Tests for find_cleanable_files function."""

    def test_empty_project(self, temp_project: Path):
        """Test that empty project has no files to clean."""
        result = find_cleanable_files(temp_project)

        assert len(result.to_delete) == 0
        assert len(result.to_keep) == 3  # project, main pcb, main sch

    def test_detects_old_pcb_versions(self, project_with_old_versions: Path):
        """Test detection of old PCB versions."""
        result = find_cleanable_files(project_with_old_versions)

        pcb_versions = result.by_category("pcb_version")
        assert len(pcb_versions) == 4

        # Check specific patterns matched
        names = {f.path.name for f in pcb_versions}
        assert "test_project-v1.kicad_pcb" in names
        assert "test_project-v2.kicad_pcb" in names
        assert "test_project-routed.kicad_pcb" in names
        assert "test_project-autorouted.kicad_pcb" in names

    def test_detects_stale_reports(self, project_with_stale_reports: Path):
        """Test detection of stale reports."""
        result = find_cleanable_files(project_with_stale_reports)

        stale_reports = result.by_category("stale_report")
        # Should detect all versioned reports, keeping the most recent
        assert len(stale_reports) >= 2

    def test_detects_backup_files(self, project_with_backups: Path):
        """Test detection of backup files."""
        result = find_cleanable_files(project_with_backups)

        backups = result.by_category("backup")
        assert len(backups) == 4

        names = {f.path.name for f in backups}
        assert "test_project.kicad_sch.bak" in names
        assert "test_project.kicad_pcb~" in names
        assert "test_project-bak.kicad_pcb" in names
        assert "test_project.kicad_pcb.lck" in names

    def test_protects_main_files(self, project_with_old_versions: Path):
        """Test that main project files are protected."""
        result = find_cleanable_files(project_with_old_versions)

        protected_names = {f.path.name for f in result.to_keep}
        assert "test_project.kicad_pro" in protected_names
        assert "test_project.kicad_pcb" in protected_names
        assert "test_project.kicad_sch" in protected_names

    def test_file_not_found(self, tmp_path: Path):
        """Test error handling for missing project file."""
        with pytest.raises(FileNotFoundError):
            find_cleanable_files(tmp_path / "nonexistent.kicad_pro")

    def test_invalid_project_extension(self, tmp_path: Path):
        """Test error handling for wrong file extension."""
        txt_file = tmp_path / "test.txt"
        txt_file.write_text("not a project")
        with pytest.raises(ValueError):
            find_cleanable_files(txt_file)


class TestFormatOutput:
    """Tests for output formatting functions."""

    def test_format_text_empty(self, temp_project: Path):
        """Test text output for empty project."""
        result = find_cleanable_files(temp_project)
        output = format_output_text(result)

        assert "No files to clean up" in output
        assert "test_project" in output

    def test_format_text_with_files(self, project_with_old_versions: Path):
        """Test text output with files to clean."""
        result = find_cleanable_files(project_with_old_versions)
        output = format_output_text(result)

        assert "Would delete" in output
        assert "old PCB versions" in output
        assert "Would keep" in output
        assert "Space savings" in output

    def test_format_json(self, project_with_old_versions: Path):
        """Test JSON output."""
        result = find_cleanable_files(project_with_old_versions)
        output = format_output_json(result)

        # Should be valid JSON
        data = json.loads(output)

        assert "project_name" in data
        assert "to_delete" in data
        assert "to_keep" in data
        assert "total_size_bytes" in data
        assert isinstance(data["to_delete"], list)


class TestCleanCLI:
    """Tests for the clean command CLI."""

    def test_dry_run_default(self, project_with_old_versions: Path, capsys):
        """Test that dry-run is the default behavior."""
        # Run without --dry-run flag, should still not delete
        result = clean_main([str(project_with_old_versions)])
        assert result == 0

        # Files should still exist
        project_dir = project_with_old_versions.parent
        assert (project_dir / "test_project-v1.kicad_pcb").exists()

    def test_dry_run_explicit(self, project_with_old_versions: Path, capsys):
        """Test explicit dry-run flag."""
        result = clean_main([str(project_with_old_versions), "--dry-run"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Would delete" in captured.out

    def test_json_format(self, project_with_old_versions: Path, capsys):
        """Test JSON output format."""
        result = clean_main([str(project_with_old_versions), "--format", "json"])
        assert result == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "to_delete" in data

    def test_force_deletes_files(self, project_with_old_versions: Path, capsys):
        """Test that --force actually deletes files."""
        project_dir = project_with_old_versions.parent

        # Verify files exist before
        assert (project_dir / "test_project-v1.kicad_pcb").exists()

        result = clean_main([str(project_with_old_versions), "--force"])
        assert result == 0

        # Files should be deleted
        assert not (project_dir / "test_project-v1.kicad_pcb").exists()
        assert not (project_dir / "test_project-v2.kicad_pcb").exists()

        # Main files should still exist
        assert (project_dir / "test_project.kicad_pcb").exists()

        captured = capsys.readouterr()
        assert "Deleted" in captured.out

    def test_deep_clean(self, temp_project: Path, capsys):
        """Test --deep flag includes generated files."""
        project_dir = temp_project.parent

        # Create some generated files
        (project_dir / "test-F.Cu.gbr").write_text("gerber")
        (project_dir / "test.drl").write_text("drill")
        (project_dir / "test_pos.csv").write_text("position")

        result = clean_main([str(temp_project), "--deep"])
        assert result == 0

        captured = capsys.readouterr()
        assert "generated" in captured.out.lower() or "Would delete" in captured.out

    def test_missing_project_file(self, tmp_path: Path, capsys):
        """Test error handling for missing project file."""
        result = clean_main([str(tmp_path / "nonexistent.kicad_pro")])
        assert result == 1

        captured = capsys.readouterr()
        assert "Error" in captured.err

    def test_verbose_output(self, project_with_old_versions: Path, capsys):
        """Test verbose output flag."""
        result = clean_main([str(project_with_old_versions), "-v"])
        assert result == 0


class TestIntegration:
    """Integration tests for clean command with unified CLI."""

    def test_unified_cli_clean_command(self, project_with_old_versions: Path, capsys):
        """Test clean command through unified CLI."""
        from kicad_tools.cli import main

        result = main(["clean", str(project_with_old_versions), "--dry-run"])
        assert result == 0

        captured = capsys.readouterr()
        assert "Would delete" in captured.out or "No files to clean" in captured.out
