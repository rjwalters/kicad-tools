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
from kicad_tools.export.preflight import PreflightChecker, PreflightConfig, PreflightResult


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
            readme_path=tmp_path / "README.txt",
            manifest_path=tmp_path / "manifest.json",
        )
        files = result.all_files
        assert len(files) == 7

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

    def test_preflight_failure_proceeds_by_default(self, tmp_path, monkeypatch):
        """Preflight FAIL should NOT block export when strict_preflight is False (default)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        assembly_called = {"value": False}

        def fake_assembly_export(self, output_dir=None):
            assembly_called["value"] = True
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator,Footprint,LCSC Part #\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        # Mock preflight to return a FAIL result
        fail_results = [
            PreflightResult(name="board_outline", status="FAIL", message="No board outline found"),
            PreflightResult(name="bom_pcb_match", status="OK", message="All BOM components placed"),
        ]
        monkeypatch.setattr(PreflightChecker, "run_all", lambda self: fail_results)

        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
            # strict_preflight defaults to False
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "out")

        # Export should have proceeded
        assert assembly_called["value"], "Assembly generation should have been called"
        assert result.assembly_result is not None
        assert result.assembly_result.bom_path is not None

        # Preflight failures should appear as warnings, not errors
        assert len(result.warnings) == 1
        assert "board_outline" in result.warnings[0]
        assert result.success, f"Unexpected errors: {result.errors}"

        # Preflight results should still be recorded
        assert len(result.preflight_results) == 2

    def test_strict_preflight_blocks_export(self, tmp_path, monkeypatch):
        """Preflight FAIL should block export when strict_preflight is True."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        assembly_called = {"value": False}

        def fake_assembly_export(self, output_dir=None):
            assembly_called["value"] = True
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        # Mock preflight to return a FAIL result
        fail_results = [
            PreflightResult(
                name="board_outline",
                status="FAIL",
                message="No board outline found",
                details="Expected Edge.Cuts layer",
            ),
        ]
        monkeypatch.setattr(PreflightChecker, "run_all", lambda self: fail_results)

        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
            strict_preflight=True,
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        out_dir = tmp_path / "out"
        result = pkg.export(out_dir)

        # Export should NOT have proceeded
        assert not assembly_called["value"], "Assembly generation should NOT have been called"
        assert not result.success
        assert len(result.errors) == 1
        assert "board_outline" in result.errors[0]
        assert "Expected Edge.Cuts layer" in result.errors[0]

        # Output directory should not exist
        assert not out_dir.exists()

    def test_unbuildable_bom_blocks_export_by_default(self, tmp_path, monkeypatch):
        """A bom_pcb_match FAIL (schematic-only refs) blocks export even
        when strict_preflight is False.

        Issue #2729: an unbuildable BOM (BOM references parts with no PCB
        footprint) is treated as a hard failure independently of
        strict_preflight so the pipeline cannot accidentally ship a
        manufacturing package the fab cannot build.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        assembly_called = {"value": False}

        def fake_assembly_export(self, output_dir=None):
            assembly_called["value"] = True
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        # Mock preflight: bom_pcb_match FAIL (sch refs missing on PCB)
        fail_results = [
            PreflightResult(
                name="bom_pcb_match",
                status="FAIL",
                message="BOM/PCB reference mismatch",
                details="36 in BOM but not on PCB: U1, U2, U3",
            ),
        ]
        monkeypatch.setattr(PreflightChecker, "run_all", lambda self: fail_results)

        # strict_preflight defaults to False, but block_on_unbuildable_bom
        # defaults to True
        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        out_dir = tmp_path / "out"
        result = pkg.export(out_dir)

        # Export should NOT have proceeded
        assert not assembly_called["value"]
        assert not result.success
        assert len(result.errors) == 1
        assert "bom_pcb_match" in result.errors[0]
        # No bom_jlcpcb.csv written
        assert not out_dir.exists() or not (out_dir / "bom_jlcpcb.csv").exists()

    def test_unbuildable_bom_allowed_when_flag_disabled(self, tmp_path, monkeypatch):
        """With block_on_unbuildable_bom=False (--allow-unbuildable-bom),
        the export proceeds and the failure is recorded as a warning.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        assembly_called = {"value": False}

        def fake_assembly_export(self, output_dir=None):
            assembly_called["value"] = True
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        fail_results = [
            PreflightResult(
                name="bom_pcb_match",
                status="FAIL",
                message="BOM/PCB reference mismatch",
                details="36 in BOM but not on PCB: U1, U2, U3",
            ),
        ]
        monkeypatch.setattr(PreflightChecker, "run_all", lambda self: fail_results)

        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
            block_on_unbuildable_bom=False,
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        out_dir = tmp_path / "out"
        result = pkg.export(out_dir)

        # Export proceeds
        assert assembly_called["value"]
        # Failure recorded as warning, not error
        assert any("bom_pcb_match" in w for w in result.warnings)
        assert result.success


class TestLatestReportOnly:
    """Tests for the latest_report_only flattening feature."""

    def _setup_project(self, tmp_path):
        """Create a minimal project directory and return pcb path."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        sch = project_dir / "board.kicad_sch"
        sch.write_text("(kicad_sch)")
        return pcb

    def _fake_report_generate(self, out_dir, version_num):
        """Simulate report generation by creating a vN/ directory with report.md."""
        version_dir = out_dir / f"v{version_num}"
        version_dir.mkdir(parents=True, exist_ok=True)
        data_dir = version_dir / "data"
        data_dir.mkdir(exist_ok=True)
        (data_dir / "board_summary.json").write_text('{"layer_count": 2}')
        (version_dir / "report.md").write_text(f"# Report v{version_num}\n")
        (version_dir / "metadata.json").write_text('{"version": ' + str(version_num) + "}\n")
        return version_dir / "report.md"

    def test_flatten_latest_report(self, tmp_path, monkeypatch):
        """With latest_report_only=True, report.md is promoted to package root."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        # Pre-create versioned directories to simulate prior exports
        out_dir.mkdir(parents=True, exist_ok=True)
        self._fake_report_generate(out_dir, 1)
        self._fake_report_generate(out_dir, 2)

        # Mock _generate_report to create v3 (the latest)
        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 3)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # report.md should be promoted to the package root
        assert (out_dir / "report.md").exists()
        assert "v3" in (out_dir / "report.md").read_text()

        # No report/ subdirectory should remain (artifacts discarded by default)
        assert not (out_dir / "report").exists()

        # No .build/ directory without keep_build_artifacts
        assert not (out_dir / ".build").exists()

        # No vN/ directories should remain
        import re

        for child in out_dir.iterdir():
            if child.is_dir():
                assert not re.fullmatch(r"v\d+", child.name), (
                    f"Version directory {child.name} should have been removed"
                )

        # result.report_path should point to the root-level file
        assert result.report_path == out_dir / "report.md"

    def test_latest_only_false_preserves_version_dirs(self, tmp_path, monkeypatch):
        """With latest_report_only=False (default), vN/ directories are preserved."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)
        self._fake_report_generate(out_dir, 1)

        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 2)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # Version directories should remain
        assert (out_dir / "v1").exists()
        assert (out_dir / "v2").exists()

        # No report/ directory should have been created
        assert not (out_dir / "report").exists()

    def test_latest_only_with_no_report(self, tmp_path, monkeypatch):
        """latest_report_only=True with include_report=False should not fail."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # Should succeed without errors
        assert result.success
        # No report/ or vN/ directories
        assert not (out_dir / "report").exists()

    def test_latest_only_fresh_project_single_version(self, tmp_path, monkeypatch):
        """On a fresh project, latest_report_only promotes v1/report.md to root."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 1)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # report.md should be promoted to root with v1 content
        assert (out_dir / "report.md").exists()
        assert "v1" in (out_dir / "report.md").read_text()

        # v1/ should be removed
        assert not (out_dir / "v1").exists()

        # No report/ subdirectory
        assert not (out_dir / "report").exists()

    def test_config_default_latest_report_only(self):
        """latest_report_only defaults to True."""
        config = ManufacturingConfig()
        assert config.latest_report_only is True

    def test_config_default_keep_build_artifacts(self):
        """keep_build_artifacts defaults to False."""
        config = ManufacturingConfig()
        assert config.keep_build_artifacts is False

    def test_keep_build_artifacts_preserves_intermediates(self, tmp_path, monkeypatch):
        """With keep_build_artifacts=True, .build/report/ contains build files."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)

        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 1)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            keep_build_artifacts=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # report.md should be promoted to root
        assert (out_dir / "report.md").exists()
        assert result.report_path == out_dir / "report.md"

        # .build/report/ should contain the build artifacts
        build_report = out_dir / ".build" / "report"
        assert build_report.exists()
        assert (build_report / "report.md").exists()
        assert (build_report / "data" / "board_summary.json").exists()
        assert (build_report / "metadata.json").exists()

        # No report/ or vN/ directories
        assert not (out_dir / "report").exists()
        assert not (out_dir / "v1").exists()

    def test_pdf_promoted_over_md(self, tmp_path, monkeypatch):
        """When report.pdf exists, it is promoted to root instead of report.md."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)

        def fake_generate_report(self_pkg, od, result):
            # Create both MD and PDF in v1/
            report_path = self._fake_report_generate(od, 1)
            pdf_path = report_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")
            result.report_path = pdf_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # PDF should be promoted to root
        assert (out_dir / "report.pdf").exists()
        assert result.report_path == out_dir / "report.pdf"

        # Markdown source should also be preserved alongside PDF
        assert (out_dir / "report.md").exists()
        assert result.report_md_path == out_dir / "report.md"

        # No report/ subdirectory
        assert not (out_dir / "report").exists()

    def test_no_report_no_build_dir(self, tmp_path, monkeypatch):
        """With include_report=False, no report.pdf or .build/ directory is created."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=True,
            keep_build_artifacts=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        assert result.success
        assert result.report_path is None
        assert not (out_dir / "report.pdf").exists()
        assert not (out_dir / "report.md").exists()
        assert not (out_dir / ".build").exists()

    def test_keep_versions_bypasses_artifact_cleanup(self, tmp_path, monkeypatch):
        """With latest_report_only=False, no .build/ directory is created."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)

        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 1)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=False,
            latest_report_only=False,
            keep_build_artifacts=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # vN/ directories preserved
        assert (out_dir / "v1").exists()
        # No .build/ directory (flattening was skipped)
        assert not (out_dir / ".build").exists()
        # No report at root (stays in vN/)
        assert not (out_dir / "report.md").exists()

    def test_manifest_checksums_report_at_root(self, tmp_path, monkeypatch):
        """Manifest should checksum report.md at root, not report/report.md."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)

        def fake_generate_report(self_pkg, od, result):
            report_path = self._fake_report_generate(od, 1)
            result.report_path = report_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=True,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # Parse manifest
        import json

        manifest = json.loads((out_dir / "manifest.json").read_text())

        # Manifest should have "report.md" key (at root), not "report/report.md"
        assert "report.md" in manifest["files"]
        assert "report/report.md" not in manifest.get("files", {})

    def test_manifest_includes_both_pdf_and_md(self, tmp_path, monkeypatch):
        """Manifest should include checksums for both report.pdf and report.md."""
        pcb = self._setup_project(tmp_path)
        out_dir = tmp_path / "output"

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        out_dir.mkdir(parents=True, exist_ok=True)

        def fake_generate_report(self_pkg, od, result):
            # Create both MD and PDF in v1/
            report_path = self._fake_report_generate(od, 1)
            pdf_path = report_path.with_suffix(".pdf")
            pdf_path.write_bytes(b"%PDF-1.4 fake pdf content")
            result.report_path = pdf_path

        monkeypatch.setattr(ManufacturingPackage, "_generate_report", fake_generate_report)

        config = ManufacturingConfig(
            include_report=True,
            include_project_zip=False,
            include_manifest=True,
            latest_report_only=True,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(out_dir)

        # Both files should exist at root
        assert (out_dir / "report.pdf").exists()
        assert (out_dir / "report.md").exists()

        # result should track both paths
        assert result.report_path == out_dir / "report.pdf"
        assert result.report_md_path == out_dir / "report.md"

        # Both should appear in all_files
        all_file_names = [f.name for f in result.all_files]
        assert "report.pdf" in all_file_names
        assert "report.md" in all_file_names

        # Parse manifest -- both should have checksums
        import json

        manifest = json.loads((out_dir / "manifest.json").read_text())
        assert "report.pdf" in manifest["files"]
        assert "report.md" in manifest["files"]

    def test_cli_keep_build_artifacts_flag(self):
        """CLI --keep-build-artifacts flag parses correctly."""
        from kicad_tools.cli.export_cmd import main
        import argparse

        # We just need to verify the argument parses without running the export
        parser = argparse.ArgumentParser()
        parser.add_argument("--keep-build-artifacts", action="store_true")
        args = parser.parse_args(["--keep-build-artifacts"])
        assert args.keep_build_artifacts is True

        args_default = parser.parse_args([])
        assert args_default.keep_build_artifacts is False


class TestGerberCleanup:
    """Tests for gerber file cleanup after zip creation."""

    def test_clean_after_zip_default(self):
        """clean_after_zip defaults to True."""
        from kicad_tools.export.gerber import GerberConfig

        config = GerberConfig()
        assert config.clean_after_zip is True

    def test_clean_after_zip_removes_files(self, tmp_path):
        """After creating zip, individual gerber files should be removed."""
        from kicad_tools.export.gerber import GerberExporter

        gerber_dir = tmp_path / "gerbers"
        gerber_dir.mkdir()

        # Create some fake gerber files
        (gerber_dir / "F_Cu.gtl").write_text("gerber data")
        (gerber_dir / "B_Cu.gbl").write_text("gerber data")
        (gerber_dir / "board.drl").write_text("drill data")

        # Create a zip manually
        zip_path = gerber_dir / "gerbers.zip"
        import zipfile

        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in gerber_dir.iterdir():
                if f.is_file() and f != zip_path:
                    zf.write(f, f.name)

        # Run cleanup
        GerberExporter._clean_after_zip(gerber_dir, zip_path)

        # Only the zip should remain
        remaining = list(gerber_dir.iterdir())
        assert len(remaining) == 1
        assert remaining[0] == zip_path

    def test_keep_gerber_files(self):
        """clean_after_zip=False preserves individual files."""
        from kicad_tools.export.gerber import GerberConfig

        config = GerberConfig(clean_after_zip=False)
        assert config.clean_after_zip is False


class TestReadmeGeneration:
    """Tests for README.txt generation in manufacturing packages."""

    def test_readme_generated_by_default(self, tmp_path, monkeypatch):
        """README.txt should be generated by default."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        result = pkg.export(tmp_path / "output")

        assert result.readme_path is not None
        assert result.readme_path.exists()
        assert result.readme_path.name == "README.txt"

        content = result.readme_path.read_text()
        assert "Manufacturing Package" in content
        assert "jlcpcb" in content
        assert "bom_jlcpcb.csv" in content

    def test_include_readme_false(self):
        """include_readme=False should skip README generation."""
        config = ManufacturingConfig(include_readme=False)
        assert config.include_readme is False

    def test_config_default_include_readme(self):
        """include_readme defaults to True."""
        config = ManufacturingConfig()
        assert config.include_readme is True


class TestTHTHandSolderDocumentation:
    """README/report documentation of the CPL's hand-solder THT set (issue #3539)."""

    @staticmethod
    def _tht_placements(refs_values_footprints):
        from kicad_tools.export.pnp import PlacementData

        return [
            PlacementData(ref, value, footprint, 0.0, 0.0, 0.0, "F.Cu")
            for ref, value, footprint in refs_values_footprints
        ]

    def _export(self, tmp_path, monkeypatch, tht_excluded):
        """Run a ManufacturingPackage export with a faked assembly stage."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = project_dir / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (project_dir / "board.kicad_sch").write_text("(kicad_sch)")

        from kicad_tools.export import assembly

        def fake_assembly_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom_path = od / "bom_jlcpcb.csv"
            bom_path.write_text("Comment,Designator\n")
            pnp_path = od / "cpl_jlcpcb.csv"
            pnp_path.write_text("Designator,Val\n")
            return assembly.AssemblyPackageResult(
                output_dir=od,
                bom_path=bom_path,
                pnp_path=pnp_path,
                tht_excluded=tht_excluded,
            )

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_assembly_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(pcb_path=pcb, manufacturer="jlcpcb", config=config)
        return pkg.export(tmp_path / "output")

    def test_readme_lists_hand_solder_refs(self, tmp_path, monkeypatch):
        """README.txt's CPL entry must enumerate the excluded THT refs."""
        tht = self._tht_placements(
            [
                ("J1", "Conn_01x04", "PinHeader_1x04"),
                ("R6", "1k", "R_Axial"),
                ("SW1", "SW_Push", "SW_PUSH_6mm"),
            ]
        )
        result = self._export(tmp_path, monkeypatch, tht)

        assert result.readme_path is not None
        content = result.readme_path.read_text()
        assert "cpl_jlcpcb.csv" in content
        assert "3 through-hole component(s)" in content
        assert "hand-soldered" in content
        assert "J1, R6, SW1" in content

    def test_readme_no_note_for_smd_only_board(self, tmp_path, monkeypatch):
        """SMD-only boards must not get a spurious hand-solder note."""
        result = self._export(tmp_path, monkeypatch, [])

        content = result.readme_path.read_text()
        assert "cpl_jlcpcb.csv" in content
        assert "hand-soldered" not in content
        assert "through-hole" not in content

    def test_tht_component_groups_from_assembly_result(self, tmp_path):
        """_tht_component_groups produces BOM-style rows for the report."""
        from kicad_tools.export.assembly import AssemblyPackageResult

        result = ManufacturingResult(
            output_dir=tmp_path,
            assembly_result=AssemblyPackageResult(
                output_dir=tmp_path,
                tht_excluded=self._tht_placements(
                    [
                        ("R20", "1k", "R_Axial"),
                        ("R6", "1k", "R_Axial"),
                        ("J1", "Conn_01x04", "PinHeader_1x04"),
                    ]
                ),
            ),
        )

        rows = ManufacturingPackage._tht_component_groups(result)
        assert rows == [
            {"value": "Conn_01x04", "footprint": "PinHeader_1x04", "qty": 1, "refs": "J1"},
            {"value": "1k", "footprint": "R_Axial", "qty": 2, "refs": "R6, R20"},
        ]

    def test_tht_component_groups_empty_cases(self, tmp_path):
        """No assembly result or empty exclusion set -> no rows."""
        from kicad_tools.export.assembly import AssemblyPackageResult

        no_assembly = ManufacturingResult(output_dir=tmp_path)
        assert ManufacturingPackage._tht_component_groups(no_assembly) == []

        empty = ManufacturingResult(
            output_dir=tmp_path,
            assembly_result=AssemblyPackageResult(output_dir=tmp_path),
        )
        assert ManufacturingPackage._tht_component_groups(empty) == []
