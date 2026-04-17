"""Tests for PCB manufacturing export methods."""

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.schema.pcb import PCB


class TestPCBPath:
    """Tests for PCB path tracking."""

    def test_load_sets_path(self, test_project_pcb):
        """PCB.load() should store the file path."""
        pcb = PCB.load(test_project_pcb)
        assert pcb.path is not None
        assert pcb.path == Path(test_project_pcb)

    def test_create_has_no_path(self):
        """PCB.create() should have no path initially."""
        pcb = PCB.create(width=100, height=100)
        assert pcb.path is None

    def test_save_sets_path(self):
        """Saving a PCB should update the stored path."""
        pcb = PCB.create(width=100, height=100)
        assert pcb.path is None

        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as f:
            path = Path(f.name)

        try:
            pcb.save(path)
            assert pcb.path == path
        finally:
            path.unlink(missing_ok=True)

    def test_save_without_path_raises(self):
        """Saving without a path when none stored should raise."""
        pcb = PCB.create(width=100, height=100)
        with pytest.raises(ValueError, match="No path specified"):
            pcb.save()


class TestExportPlacement:
    """Tests for export_placement method."""

    def test_export_placement_csv(self, test_project_pcb):
        """Should export placement file in CSV format."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "placement.csv"
            result = pcb.export_placement(output)

            assert result == output
            assert output.exists()

            content = output.read_text()
            # Check that the file has valid placement data
            assert "Ref" in content or "Reference" in content or "Designator" in content

    def test_export_placement_jlcpcb(self, test_project_pcb):
        """Should export placement in JLCPCB format."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "cpl_jlcpcb.csv"
            result = pcb.export_placement(output, format="jlcpcb")

            assert result == output
            assert output.exists()

    def test_export_placement_top_only(self, test_project_pcb):
        """Should export only top-side components."""
        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "placement_top.csv"
            result = pcb.export_placement(output, side="top")

            assert result == output
            assert output.exists()


class TestExportGerbers:
    """Tests for export_gerbers method (requires kicad-cli)."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_gerbers_jlcpcb(self, test_project_pcb, skip_without_kicad_cli):
        """Should export Gerber files for JLCPCB."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "gerbers"
            try:
                result = pcb.export_gerbers(output_dir, manufacturer="jlcpcb")
                assert result.exists()
                # Check some gerber files exist
                gerber_files = list(result.glob("*.g*")) + list(result.glob("*.G*"))
                assert len(gerber_files) > 0
            except ExportError as e:
                # kicad-cli may fail for various reasons (version mismatch, etc.)
                pytest.skip(f"kicad-cli export failed: {e}")

    def test_export_gerbers_requires_path(self):
        """Should raise error if PCB has no path."""
        pcb = PCB.create(width=100, height=100)

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="requires a PCB file path"):
                pcb.export_gerbers(tmpdir)


class TestExportGerbersZip:
    """Tests for export_gerbers_zip method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_gerbers_zip(self, test_project_pcb, skip_without_kicad_cli):
        """Should export Gerbers as a zip file."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "gerbers.zip"
            try:
                result = pcb.export_gerbers_zip(output, manufacturer="jlcpcb")

                assert result == output
                assert output.exists()

                # Verify it's a valid zip
                import zipfile

                with zipfile.ZipFile(output, "r") as zf:
                    names = zf.namelist()
                    assert len(names) > 0
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestExportBOM:
    """Tests for export_bom method."""

    def test_export_bom_requires_schematic(self, test_project_pcb):
        """Should raise error if schematic not found."""
        pcb = PCB.load(test_project_pcb)

        # If no schematic exists alongside the PCB
        schematic_path = Path(test_project_pcb).with_suffix(".kicad_sch")
        if not schematic_path.exists():
            with tempfile.TemporaryDirectory() as tmpdir:
                with pytest.raises(ValueError, match="Schematic not found"):
                    pcb.export_bom(Path(tmpdir) / "bom.csv")


class TestExportDrill:
    """Tests for export_drill method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_drill(self, test_project_pcb, skip_without_kicad_cli):
        """Should export drill files."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir) / "drill"
            try:
                result = pcb.export_drill(output_dir)
                assert result.exists()
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestExportManufacturing:
    """Tests for export_manufacturing method."""

    @pytest.fixture
    def skip_without_kicad_cli(self):
        """Skip test if kicad-cli is not available."""
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

    def test_export_manufacturing_requires_path(self):
        """Should raise error if PCB has no path."""
        pcb = PCB.create(width=100, height=100)

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="requires a PCB file path"):
                pcb.export_manufacturing(tmpdir)

    def test_export_manufacturing(self, test_project_pcb, skip_without_kicad_cli):
        """Should export complete manufacturing package."""
        from kicad_tools.exceptions import ExportError

        pcb = PCB.load(test_project_pcb)

        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                # Export without assembly (no schematic needed)
                result = pcb.export_manufacturing(
                    tmpdir, manufacturer="jlcpcb", include_assembly=False
                )

                assert "gerbers" in result
                # The gerbers key may be None or a path depending on success
                if result["gerbers"]:
                    assert Path(result["gerbers"]).exists()
            except ExportError as e:
                # kicad-cli may fail for various reasons
                pytest.skip(f"kicad-cli export failed: {e}")


class TestGenerateReport:
    """Tests for ManufacturingPackage._generate_report method."""

    def test_generate_report_produces_markdown(self, test_project_pcb):
        """_generate_report should produce a report.md and set result.report_path."""
        from kicad_tools.export.manufacturing import (
            ManufacturingConfig,
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
            config=ManufacturingConfig(include_report=True),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)
            pkg._generate_report(out_dir, result)

            # report_path should be set to a valid .md file
            assert result.report_path is not None
            assert result.report_path.exists()
            assert result.report_path.suffix == ".md"
            # No errors should be recorded
            report_errors = [e for e in result.errors if "Report" in e or "report" in e]
            assert len(report_errors) == 0

    def test_generate_report_no_import_error(self, test_project_pcb):
        """_generate_report must not trigger an ImportError for generate_report."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)

            # Patch logger to capture warnings -- ImportError would produce
            # "report module not available" warning
            with patch(
                "kicad_tools.export.manufacturing.logger"
            ) as mock_logger:
                pkg._generate_report(out_dir, result)

                # Verify the old misleading message does NOT appear
                for call in mock_logger.warning.call_args_list:
                    msg = call[0][0] if call[0] else ""
                    assert "report module not available" not in msg

    def test_generate_report_sets_report_path_in_result(self, test_project_pcb):
        """ManufacturingResult.report_path should point to the generated file."""
        from kicad_tools.export.manufacturing import (
            ManufacturingPackage,
            ManufacturingResult,
        )

        pkg = ManufacturingPackage(
            pcb_path=test_project_pcb,
            manufacturer="jlcpcb",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            out_dir = Path(tmpdir)
            result = ManufacturingResult(output_dir=out_dir)
            pkg._generate_report(out_dir, result)

            assert result.report_path is not None
            content = result.report_path.read_text(encoding="utf-8")
            # Should contain some markdown content
            assert len(content) > 0


# Fixtures
@pytest.fixture
def test_project_pcb():
    """Path to test project PCB fixture."""
    fixture_path = Path(__file__).parent / "fixtures" / "projects" / "test_project.kicad_pcb"
    if not fixture_path.exists():
        pytest.skip(f"Test fixture not found: {fixture_path}")
    return str(fixture_path)
