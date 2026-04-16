"""Tests for the pre-flight validation checker."""

import json
from pathlib import Path

from kicad_tools.export.preflight import (
    PreflightChecker,
    PreflightConfig,
    PreflightResult,
)

# ---------------------------------------------------------------------------
# PreflightResult tests
# ---------------------------------------------------------------------------


class TestPreflightResult:
    """Tests for the PreflightResult dataclass."""

    def test_to_dict_basic(self):
        r = PreflightResult(name="test", status="OK", message="all good")
        d = r.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "OK"
        assert d["message"] == "all good"
        assert "details" not in d

    def test_to_dict_with_details(self):
        r = PreflightResult(name="test", status="FAIL", message="bad", details="extra info")
        d = r.to_dict()
        assert d["details"] == "extra info"

    def test_status_values(self):
        for status in ("OK", "WARN", "FAIL"):
            r = PreflightResult(name="t", status=status, message="m")
            assert r.status == status


# ---------------------------------------------------------------------------
# PreflightChecker -- PCB file checks
# ---------------------------------------------------------------------------


class TestPreflightPCBFile:
    """Tests for the PCB file check."""

    def test_missing_pcb_fails(self, tmp_path):
        checker = PreflightChecker(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "FAIL"

    def test_valid_pcb_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "OK"

    def test_unparseable_pcb_fails(self, tmp_path):
        pcb = tmp_path / "broken.kicad_pcb"
        pcb.write_text("this is not valid s-expression")
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        pcb_result = _find_result(results, "pcb_file")
        assert pcb_result is not None
        assert pcb_result.status == "FAIL"


# ---------------------------------------------------------------------------
# PreflightChecker -- schematic checks
# ---------------------------------------------------------------------------


class TestPreflightSchematic:
    """Tests for the schematic file check."""

    def test_no_schematic_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=None,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status in ("OK", "WARN")

    def test_missing_schematic_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=tmp_path / "nonexistent.kicad_sch",
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "WARN"

    def test_existing_schematic_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "OK"

    def test_auto_detect_schematic(self, tmp_path):
        """If schematic_path is None, auto-detect from PCB name."""
        pcb = _create_minimal_pcb(tmp_path)
        # Create matching schematic
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')
        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=None,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        sch_result = _find_result(results, "schematic_file")
        assert sch_result is not None
        assert sch_result.status == "OK"
        assert "auto-detected" in sch_result.message


# ---------------------------------------------------------------------------
# PreflightChecker -- board outline checks
# ---------------------------------------------------------------------------


class TestPreflightBoardOutline:
    """Tests for board outline validation."""

    def test_no_outline_fails(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=False)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "FAIL"

    def test_closed_outline_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "OK"

    def test_gr_rect_outline_ok(self, tmp_path):
        """Board outline defined via gr_rect on Edge.Cuts should pass."""
        pcb_content = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect (start 0 0) (end 50 50)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
)
"""
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb_path.write_text(pcb_content)
        checker = PreflightChecker(
            pcb_path=pcb_path,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert outline_result.status == "OK"

    def test_no_outline_error_message_empty(self, tmp_path):
        """When Edge.Cuts has no content, message says 'No board outline found'."""
        pcb = _create_minimal_pcb(tmp_path, include_outline=False)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        outline_result = _find_result(results, "board_outline")
        assert outline_result is not None
        assert "No board outline found" in outline_result.message


# ---------------------------------------------------------------------------
# PreflightChecker -- board dimensions checks
# ---------------------------------------------------------------------------


class TestPreflightBoardDimensions:
    """Tests for board dimension validation."""

    def test_normal_size_ok(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        dim_result = _find_result(results, "board_dimensions")
        assert dim_result is not None
        assert dim_result.status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- drill holes check
# ---------------------------------------------------------------------------


class TestPreflightDrillHoles:
    """Tests for drill holes presence check."""

    def test_no_drill_holes_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path, include_outline=True)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drill_result = _find_result(results, "drill_holes")
        assert drill_result is not None
        # A minimal board with no components: WARN expected
        assert drill_result.status == "WARN"

    def test_with_vias_ok(self, tmp_path):
        pcb = _create_pcb_with_via(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drill_result = _find_result(results, "drill_holes")
        assert drill_result is not None
        assert drill_result.status == "OK"


# ---------------------------------------------------------------------------
# PreflightChecker -- DRC check
# ---------------------------------------------------------------------------


class TestPreflightDRC:
    """Tests for the DRC check."""

    def test_no_report_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_erc=True),
        )
        results = checker.run_all()
        drc_result = _find_result(results, "drc")
        assert drc_result is not None
        assert drc_result.status == "WARN"

    def test_skip_drc(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        drc_result = _find_result(results, "drc")
        assert drc_result is None  # Should not appear when skipped


# ---------------------------------------------------------------------------
# PreflightChecker -- ERC check
# ---------------------------------------------------------------------------


class TestPreflightERC:
    """Tests for the ERC check."""

    def test_no_report_warns(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True),
        )
        results = checker.run_all()
        erc_result = _find_result(results, "erc")
        assert erc_result is not None
        assert erc_result.status == "WARN"

    def test_skip_erc(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        results = checker.run_all()
        erc_result = _find_result(results, "erc")
        assert erc_result is None


# ---------------------------------------------------------------------------
# PreflightChecker -- BOM/PCB match severity
# ---------------------------------------------------------------------------


class TestPreflightBomPcbMatchSeverity:
    """Tests for bom_pcb_match FAIL vs WARN based on mismatch direction."""

    def _make_checker_with_mocks(self, monkeypatch, bom_items, pcb_refs):
        """Create a PreflightChecker with mocked BOM and PCB data.

        Args:
            monkeypatch: pytest monkeypatch fixture.
            bom_items: list of BOMItem instances for the BOM.
            pcb_refs: list of reference strings for PCB footprints.
        """
        from unittest.mock import MagicMock

        from kicad_tools.schema.bom import BOM

        checker = PreflightChecker.__new__(PreflightChecker)
        checker.config = PreflightConfig()

        # Mock _pcb with footprints that have .reference attributes
        mock_pcb = MagicMock()
        fps = []
        for ref in pcb_refs:
            fp = MagicMock()
            fp.reference = ref
            fps.append(fp)
        mock_pcb.footprints = fps
        checker._pcb = mock_pcb

        # Mock _load_bom to return our BOM
        bom = BOM(items=bom_items)
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        return checker

    def _make_bom_item(self, reference, **kwargs):
        from kicad_tools.schema.bom import BOMItem

        defaults = {
            "value": "100nF",
            "footprint": "C_0402",
            "lib_id": "Device:C",
        }
        defaults.update(kwargs)
        return BOMItem(reference=reference, **defaults)

    def test_all_match_ok(self, monkeypatch):
        """When BOM and PCB refs match exactly, status is OK."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "R1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"
        assert "2 components" in result.message

    def test_unplaced_components_fail(self, monkeypatch):
        """BOM refs not on PCB (unplaced) should produce FAIL."""
        items = [
            self._make_bom_item("C1"),
            self._make_bom_item("R1"),
            self._make_bom_item("U1"),
        ]
        # PCB only has C1 -- R1 and U1 are unplaced
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert "in BOM but not on PCB" in result.details

    def test_orphaned_footprints_warn(self, monkeypatch):
        """PCB refs not in BOM (orphaned) should produce WARN, not FAIL."""
        items = [self._make_bom_item("C1")]
        # PCB has C1 plus extra MH1 (mounting hole)
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"
        assert "on PCB but not in BOM" in result.details

    def test_both_directions_fail_takes_precedence(self, monkeypatch):
        """When both unplaced and orphaned exist, FAIL takes precedence."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        # PCB has C1 and MH1 -- R1 is unplaced, MH1 is orphaned
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert "in BOM but not on PCB" in result.details
        assert "on PCB but not in BOM" in result.details

    def test_virtual_and_dnp_excluded(self, monkeypatch):
        """Virtual and DNP items should not cause mismatches."""
        items = [
            self._make_bom_item("C1"),
            # Power symbol (virtual -- lib_id starts with "power:")
            self._make_bom_item("PWR1", lib_id="power:VCC"),
            # DNP component
            self._make_bom_item("R99", dnp=True),
        ]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "OK"

    def test_empty_bom_with_pcb_warns(self, monkeypatch):
        """Empty BOM with PCB footprints should WARN (orphaned footprints)."""
        checker = self._make_checker_with_mocks(monkeypatch, [], ["C1", "R1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"

    def test_bom_with_empty_pcb_fails(self, monkeypatch):
        """BOM items with no PCB footprints should FAIL (unplaced)."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, [])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"

    def test_has_failures_blocks_export(self, monkeypatch):
        """Verify that FAIL from bom_pcb_match is detected by has_failures."""
        items = [self._make_bom_item("C1"), self._make_bom_item("R1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "FAIL"
        assert PreflightChecker.has_failures([result]) is True

    def test_orphaned_only_does_not_block_export(self, monkeypatch):
        """Verify that WARN from orphaned footprints does NOT block export."""
        items = [self._make_bom_item("C1")]
        checker = self._make_checker_with_mocks(monkeypatch, items, ["C1", "MH1"])

        result = checker._check_bom_footprint_match()
        assert result.status == "WARN"
        assert PreflightChecker.has_failures([result]) is False


# ---------------------------------------------------------------------------
# PreflightChecker -- skip all
# ---------------------------------------------------------------------------


class TestPreflightSkipAll:
    """Tests for skip_all config."""

    def test_skip_all_returns_empty(self, tmp_path):
        pcb = _create_minimal_pcb(tmp_path)
        checker = PreflightChecker(
            pcb_path=pcb,
            config=PreflightConfig(skip_all=True),
        )
        results = checker.run_all()
        assert results == []


# ---------------------------------------------------------------------------
# PreflightChecker -- static helpers
# ---------------------------------------------------------------------------


class TestPreflightStaticHelpers:
    """Tests for static helper methods."""

    def test_has_failures_true(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="FAIL", message="bad"),
        ]
        assert PreflightChecker.has_failures(results) is True

    def test_has_failures_false(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="WARN", message="meh"),
        ]
        assert PreflightChecker.has_failures(results) is False

    def test_has_warnings_true(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
            PreflightResult(name="b", status="WARN", message="meh"),
        ]
        assert PreflightChecker.has_warnings(results) is True

    def test_has_warnings_false(self):
        results = [
            PreflightResult(name="a", status="OK", message="ok"),
        ]
        assert PreflightChecker.has_warnings(results) is False

    def test_has_failures_empty(self):
        assert PreflightChecker.has_failures([]) is False

    def test_has_warnings_empty(self):
        assert PreflightChecker.has_warnings([]) is False


# ---------------------------------------------------------------------------
# Integration -- manufacturing package with preflight
# ---------------------------------------------------------------------------


class TestPreflightManufacturingIntegration:
    """Tests for preflight integration in ManufacturingPackage."""

    def test_export_with_preflight_ok(self, tmp_path, monkeypatch):
        """Manufacturing export should succeed when preflight passes."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success, f"Errors: {result.errors}"
        assert len(result.preflight_results) > 0
        assert not PreflightChecker.has_failures(result.preflight_results)

    def test_export_blocked_by_preflight_fail(self, tmp_path):
        """Manufacturing export should fail when preflight has FAIL results."""
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # Non-existent PCB will cause a FAIL
        pkg = ManufacturingPackage(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert not result.success
        assert any("Preflight FAIL" in e for e in result.errors)

    def test_skip_preflight_bypasses_checks(self, tmp_path, monkeypatch):
        """With skip_all=True, preflight checks are not run."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_all=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success
        assert result.preflight_results == []

    def test_preflight_results_in_manifest(self, tmp_path, monkeypatch):
        """Preflight results should be included in manifest.json."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly
        from kicad_tools.export.manufacturing import ManufacturingConfig, ManufacturingPackage
        from kicad_tools.export.preflight import PreflightConfig

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        config = ManufacturingConfig(
            include_report=False,
            preflight=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        pkg = ManufacturingPackage(
            pcb_path=pcb,
            manufacturer="jlcpcb",
            config=config,
        )
        result = pkg.export(tmp_path / "output")

        assert result.success
        assert result.manifest_path is not None
        assert result.manifest_path.exists()

        manifest = json.loads(result.manifest_path.read_text())
        assert "preflight" in manifest
        assert isinstance(manifest["preflight"], list)
        assert len(manifest["preflight"]) > 0
        # Each entry should have name, status, message
        for entry in manifest["preflight"]:
            assert "name" in entry
            assert "status" in entry
            assert "message" in entry


# ---------------------------------------------------------------------------
# CLI integration tests
# ---------------------------------------------------------------------------


class TestPreflightCLI:
    """Tests for preflight CLI flags."""

    def test_skip_preflight_flag(self, tmp_path, monkeypatch):
        """--skip-preflight should skip all checks."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-preflight",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

    def test_skip_drc_flag(self, tmp_path, monkeypatch):
        """--skip-drc should skip only DRC."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_sch").write_text(
            '(kicad_sch (version 20231120) (generator "eeschema"))'
        )
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            bom = od / "bom_jlcpcb.csv"
            bom.write_text("Comment,Designator\n")
            return assembly.AssemblyPackageResult(output_dir=od, bom_path=bom)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-drc",
                "--skip-erc",
                "--no-report",
                "--no-project-zip",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

    def test_json_format_output(self, tmp_path, monkeypatch, capsys):
        """--format json should produce JSON output."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        pcb = _create_minimal_pcb(project_dir, include_outline=True)
        (project_dir / "board.kicad_pro").write_text("{}")

        from kicad_tools.export import assembly

        def fake_export(self, output_dir=None):
            od = Path(output_dir) if output_dir else self.config.output_dir
            od.mkdir(parents=True, exist_ok=True)
            return assembly.AssemblyPackageResult(output_dir=od)

        monkeypatch.setattr(assembly.AssemblyPackage, "export", fake_export)

        from kicad_tools.cli.export_cmd import main as export_main

        rc = export_main(
            [
                str(pcb),
                "--skip-preflight",
                "--no-report",
                "--no-project-zip",
                "--format",
                "json",
                "-o",
                str(tmp_path / "out"),
            ]
        )
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "success" in data
        assert data["success"] is True

    def test_parser_recognizes_preflight_flags(self):
        """The main parser should recognize preflight flags."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "export",
                "board.kicad_pcb",
                "--skip-preflight",
                "--skip-drc",
                "--skip-erc",
                "--drc-report",
                "/tmp/drc.json",
                "--erc-report",
                "/tmp/erc.json",
                "--format",
                "json",
            ]
        )
        assert args.export_skip_preflight is True
        assert args.export_skip_drc is True
        assert args.export_skip_erc is True
        assert args.export_drc_report == "/tmp/drc.json"
        assert args.export_erc_report == "/tmp/erc.json"
        assert args.export_format == "json"


# ---------------------------------------------------------------------------
# PreflightChecker -- BOM/CPL match check
# ---------------------------------------------------------------------------


class TestPreflightBomCplMatch:
    """Tests for the BOM/CPL cross-reference check."""

    def test_bom_cpl_match_ok(self, tmp_path, monkeypatch):
        """When BOM refs and CPL-eligible PCB refs match, result is OK."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "R2", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # Parse PCB first
        checker.run_all()

        # Monkeypatch _load_bom to return matching refs
        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.name == "bom_cpl_match"
        assert result.status == "OK"
        assert "3 components" in result.message

    def test_bom_cpl_match_bom_only(self, tmp_path, monkeypatch):
        """Items in BOM but not in CPL should cause FAIL."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="R2", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "FAIL"
        assert "R2" in result.details
        assert "in BOM but not in CPL" in result.details

    def test_bom_cpl_match_cpl_only(self, tmp_path, monkeypatch):
        """Items in CPL but not in BOM should cause FAIL."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "R2", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "FAIL"
        assert "in CPL but not in BOM" in result.details

    def test_bom_cpl_match_dnp_excluded(self, tmp_path, monkeypatch):
        """DNP items in BOM should be excluded from comparison."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1", "C1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(
                    reference="R2", value="10k", footprint="R_0402", lib_id="Device:R", dnp=True
                ),
                BOMItem(reference="C1", value="100nF", footprint="C_0402", lib_id="Device:C"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"
        assert "2 components" in result.message

    def test_bom_cpl_match_virtual_excluded(self, tmp_path, monkeypatch):
        """Virtual (power symbol) items in BOM should be excluded."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
                BOMItem(reference="#PWR01", value="GND", footprint="", lib_id="power:GND"),
            ]
        )
        monkeypatch.setattr(checker, "_load_bom", lambda: bom)

        result = checker._check_bom_cpl_match()
        assert result.status == "OK"

    def test_bom_cpl_match_no_pcb(self, tmp_path):
        """Without a loaded PCB, check should return WARN."""
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=tmp_path / "nonexistent.kicad_pcb",
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        # PCB won't be loaded since file doesn't exist
        result = checker._check_bom_cpl_match()
        assert result.status == "WARN"
        assert "PCB not loaded" in result.message

    def test_bom_cpl_match_bom_load_fails(self, tmp_path, monkeypatch):
        """If BOM cannot be loaded, check should return WARN."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        checker.run_all()

        monkeypatch.setattr(
            checker, "_load_bom", lambda: (_ for _ in ()).throw(RuntimeError("parse error"))
        )

        result = checker._check_bom_cpl_match()
        assert result.status == "WARN"
        assert "parse error" in result.message

    def test_bom_cpl_match_in_run_all(self, tmp_path, monkeypatch):
        """The bom_cpl_match check should appear in run_all() results."""
        pcb = _create_pcb_with_footprints(tmp_path, ["R1"])
        sch = tmp_path / "board.kicad_sch"
        sch.write_text('(kicad_sch (version 20231120) (generator "eeschema"))')

        from kicad_tools.schema.bom import BOM, BOMItem

        bom = BOM(
            items=[
                BOMItem(reference="R1", value="10k", footprint="R_0402", lib_id="Device:R"),
            ]
        )

        checker = PreflightChecker(
            pcb_path=pcb,
            schematic_path=sch,
            config=PreflightConfig(skip_drc=True, skip_erc=True),
        )
        monkeypatch.setattr(
            "kicad_tools.export.preflight.PreflightChecker._load_bom",
            lambda self: bom,
        )

        results = checker.run_all()
        cpl_result = _find_result(results, "bom_cpl_match")
        assert cpl_result is not None
        assert cpl_result.status == "OK"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_result(results: list[PreflightResult], name: str) -> PreflightResult | None:
    """Find a preflight result by check name."""
    for r in results:
        if r.name == name:
            return r
    return None


def _create_minimal_pcb(
    directory: Path,
    filename: str = "board.kicad_pcb",
    include_outline: bool = True,
) -> Path:
    """Create a minimal valid KiCad PCB file for testing."""
    outline = ""
    if include_outline:
        # A closed rectangular outline on Edge.Cuts
        outline = """
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
"""

    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
{outline}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path


def _create_pcb_with_via(directory: Path) -> Path:
    """Create a PCB with a via for drill hole testing."""
    content = """(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
  (via (at 25 25) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1))
)
"""
    pcb_path = directory / "board.kicad_pcb"
    pcb_path.write_text(content)
    return pcb_path


def _create_pcb_with_footprints(
    directory: Path,
    references: list[str],
    filename: str = "board.kicad_pcb",
) -> Path:
    """Create a PCB with footprints for BOM/CPL testing.

    Each reference in *references* becomes a simple SMD footprint on F.Cu.
    A closed board outline is included so that other checks pass.
    """
    footprints = []
    for i, ref in enumerate(references):
        x = 10.0 + i * 5.0
        footprints.append(
            f"""  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at {x} 25)
    (attr smd)
    (fp_text reference "{ref}" (at 0 -1.5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (fp_text value "10k" (at 0 1.5) (layer "F.Fab") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
    (pad "2" smd rect (at 0.5 0) (size 0.6 0.5) (layers "F.Cu" "F.Paste" "F.Mask") (net 0 ""))
  )"""
        )

    fp_block = "\n".join(footprints)
    content = f"""(kicad_pcb (version 20231014) (generator "pcbnew")
  (general
    (thickness 1.6)
    (legacy_teardrops no)
  )
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))
{fp_block}
)
"""
    pcb_path = directory / filename
    pcb_path.write_text(content)
    return pcb_path
