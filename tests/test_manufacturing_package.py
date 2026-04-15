"""Tests for the manufacturing package generator."""

import json
import zipfile
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    ManufacturingResult,
    _build_manifest,
    _create_project_zip,
    _sha256_file,
)
from kicad_tools.export.preflight import PreflightConfig


class TestSha256File:
    """Tests for _sha256_file helper."""

    def test_known_content(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello\n")
        digest = _sha256_file(f)
        # sha256("hello\n") is a well-known value
        assert len(digest) == 64
        assert digest == "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.txt"
        f.write_bytes(b"")
        digest = _sha256_file(f)
        assert len(digest) == 64


class TestCreateProjectZip:
    """Tests for _create_project_zip."""

    def test_creates_zip_with_kicad_files(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")
        (project_dir / "board.kicad_pro").write_text("{}")

        out_dir = tmp_path / "output"
        out_dir.mkdir()

        zip_path = _create_project_zip(project_dir / "board.kicad_pcb", out_dir)

        assert zip_path.exists()
        assert zip_path.name == "kicad_project.zip"

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            assert "board.kicad_pcb" in names
            assert "board.kicad_sch" in names
            assert "board.kicad_pro" in names

    def test_excludes_non_kicad_files(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text("(kicad_pcb)")
        (project_dir / "notes.txt").write_text("some notes")
        (project_dir / "README.md").write_text("readme")

        out_dir = tmp_path / "output"
        out_dir.mkdir()

        zip_path = _create_project_zip(project_dir / "board.kicad_pcb", out_dir)

        with zipfile.ZipFile(zip_path, "r") as zf:
            names = set(zf.namelist())
            assert "board.kicad_pcb" in names
            assert "notes.txt" not in names
            assert "README.md" not in names

    def test_no_kicad_files_raises(self, tmp_path):
        project_dir = tmp_path / "empty_project"
        project_dir.mkdir()

        out_dir = tmp_path / "output"
        out_dir.mkdir()

        # Create a dummy .txt file only
        (project_dir / "notes.txt").write_text("nope")

        with pytest.raises(FileNotFoundError, match="No KiCad project files"):
            _create_project_zip(project_dir / "fake.kicad_pcb", out_dir)

    def test_custom_zip_name(self, tmp_path):
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        (project_dir / "board.kicad_pcb").write_text("(kicad_pcb)")

        out_dir = tmp_path / "output"
        out_dir.mkdir()

        zip_path = _create_project_zip(project_dir / "board.kicad_pcb", out_dir, "my_project.zip")
        assert zip_path.name == "my_project.zip"


class TestManufacturingConfig:
    """Tests for ManufacturingConfig defaults."""

    def test_defaults(self):
        config = ManufacturingConfig()
        assert config.include_report is True
        assert config.include_project_zip is True
        assert config.include_manifest is True
        assert config.project_zip_name == "kicad_project.zip"
        assert config.manifest_name == "manifest.json"

    def test_inherits_assembly_config(self):
        config = ManufacturingConfig()
        # These come from AssemblyConfig
        assert config.include_bom is True
        assert config.include_pnp is True
        assert config.include_gerbers is True

    def test_override_flags(self):
        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
        )
        assert config.include_report is False
        assert config.include_project_zip is False


class TestManufacturingResult:
    """Tests for ManufacturingResult dataclass."""

    def test_success_when_no_errors(self, tmp_path):
        result = ManufacturingResult(output_dir=tmp_path)
        assert result.success is True

    def test_failure_when_errors(self, tmp_path):
        result = ManufacturingResult(
            output_dir=tmp_path,
            errors=["something broke"],
        )
        assert result.success is False

    def test_all_files_empty_when_nothing_generated(self, tmp_path):
        result = ManufacturingResult(output_dir=tmp_path)
        assert result.all_files == []

    def test_all_files_includes_everything(self, tmp_path):
        from kicad_tools.export.assembly import AssemblyPackageResult

        result = ManufacturingResult(
            output_dir=tmp_path,
            assembly_result=AssemblyPackageResult(
                output_dir=tmp_path,
                bom_path=tmp_path / "bom.csv",
                pnp_path=tmp_path / "cpl.csv",
                gerber_path=tmp_path / "gerbers.zip",
            ),
            report_path=tmp_path / "report.md",
            project_zip_path=tmp_path / "project.zip",
            manifest_path=tmp_path / "manifest.json",
        )
        files = result.all_files
        assert len(files) == 6

    def test_str_representation(self, tmp_path):
        result = ManufacturingResult(
            output_dir=tmp_path,
            manifest_path=tmp_path / "manifest.json",
        )
        text = str(result)
        assert "Manufacturing Package" in text
        assert "Manifest" in text


class TestBuildManifest:
    """Tests for _build_manifest helper."""

    def test_manifest_structure(self, tmp_path):
        # Create some files
        bom = tmp_path / "bom.csv"
        bom.write_text("Comment,Designator\n")

        from kicad_tools.export.assembly import AssemblyPackageResult

        result = ManufacturingResult(
            output_dir=tmp_path,
            assembly_result=AssemblyPackageResult(
                output_dir=tmp_path,
                bom_path=bom,
            ),
        )

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        manifest = _build_manifest(result, pcb_path, "jlcpcb")

        assert manifest["version"] == "1.0"
        assert "kicad_tools_version" in manifest
        assert "generated_at" in manifest
        assert manifest["manufacturer"] == "jlcpcb"
        assert "bom.csv" in manifest["files"]
        assert "sha256" in manifest["files"]["bom.csv"]
        assert "size" in manifest["files"]["bom.csv"]
        assert manifest["board"]["name"] == "board"

    def test_manifest_checksums_are_correct(self, tmp_path):
        f = tmp_path / "test.csv"
        f.write_text("data\n")

        from kicad_tools.export.assembly import AssemblyPackageResult

        result = ManufacturingResult(
            output_dir=tmp_path,
            assembly_result=AssemblyPackageResult(
                output_dir=tmp_path,
                bom_path=f,
            ),
        )

        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        manifest = _build_manifest(result, pcb_path, "jlcpcb")

        expected_sha = _sha256_file(f)
        assert manifest["files"]["test.csv"]["sha256"] == expected_sha
        assert manifest["files"]["test.csv"]["size"] == f.stat().st_size


class TestManufacturingPackageDryRun:
    """Tests for ManufacturingPackage dry run mode."""

    def test_dry_run_creates_no_files(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        out_dir = tmp_path / "output"

        pkg = ManufacturingPackage(
            pcb_path=pcb_path,
            manufacturer="jlcpcb",
        )
        result = pkg.export(out_dir, dry_run=True)

        # output dir should NOT have been created
        assert not out_dir.exists()

        # But result should list expected files
        assert result.assembly_result is not None
        assert result.assembly_result.bom_path is not None
        assert result.assembly_result.pnp_path is not None
        assert result.manifest_path is not None
        assert result.project_zip_path is not None

    def test_dry_run_respects_no_flags(self, tmp_path):
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")

        config = ManufacturingConfig(
            include_bom=False,
            include_report=False,
            include_project_zip=False,
        )

        pkg = ManufacturingPackage(
            pcb_path=pcb_path,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "out", dry_run=True)

        assert result.assembly_result.bom_path is None
        assert result.report_path is None
        assert result.project_zip_path is None


class TestManufacturingPackageExport:
    """Tests for ManufacturingPackage.export (integration-level).

    These tests mock the assembly pipeline (which needs kicad-cli + real PCBs)
    but exercise the project ZIP and manifest logic for real.
    """

    def test_project_zip_and_manifest(self, tmp_path, monkeypatch):
        """Test that project ZIP and manifest are generated correctly."""
        # Create a minimal project directory
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        sch = project_dir / "board.kicad_sch"
        sch.write_text("(kicad_sch)")
        pro = project_dir / "board.kicad_pro"
        pro.write_text("{}")

        out_dir = tmp_path / "output"

        # Patch AssemblyPackage.export to avoid needing kicad-cli
        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,  # skip report to avoid needing kicad-cli
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(out_dir)

        # Should succeed
        assert result.success, f"Errors: {result.errors}"

        # BOM should exist
        assert result.assembly_result is not None
        assert result.assembly_result.bom_path is not None
        assert result.assembly_result.bom_path.exists()

        # Project ZIP should exist
        assert result.project_zip_path is not None
        assert result.project_zip_path.exists()
        with zipfile.ZipFile(result.project_zip_path, "r") as zf:
            assert "board.kicad_pcb" in zf.namelist()
            assert "board.kicad_sch" in zf.namelist()
            assert "board.kicad_pro" in zf.namelist()

        # Manifest should exist
        assert result.manifest_path is not None
        assert result.manifest_path.exists()

        manifest = json.loads(result.manifest_path.read_text())
        assert manifest["version"] == "1.0"
        assert manifest["manufacturer"] == "jlcpcb"
        assert "bom_jlcpcb.csv" in manifest["files"]
        assert "kicad_project.zip" in manifest["files"]
        # Manifest should not list itself (it's written after the manifest is built)
        # Actually, since we call _build_manifest before writing manifest, the manifest
        # file doesn't exist yet, so it won't be in the files dict -- but it IS added
        # to result.manifest_path AFTER writing. So let's just verify checksums.
        bom_sha = manifest["files"]["bom_jlcpcb.csv"]["sha256"]
        assert bom_sha == _sha256_file(result.assembly_result.bom_path)

    def test_no_report_flag(self, tmp_path, monkeypatch):
        """Test that --no-report skips report generation."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "out")

        assert result.report_path is None
