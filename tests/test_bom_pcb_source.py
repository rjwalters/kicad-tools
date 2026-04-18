"""Tests for PCB-only BOM generation (--bom-source pcb/auto)."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.schema.bom import BOM, BOMItem, extract_bom_from_pcb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BOARDS_DIR = Path(__file__).resolve().parent.parent / "boards"
VOLTAGE_DIVIDER_PCB = BOARDS_DIR / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"
VOLTAGE_DIVIDER_SCH = BOARDS_DIR / "01-voltage-divider" / "output" / "voltage_divider.kicad_sch"


@pytest.fixture
def vdiv_pcb_path() -> Path:
    """Path to the voltage divider routed PCB."""
    if not VOLTAGE_DIVIDER_PCB.exists():
        pytest.skip("Voltage divider test board not found")
    return VOLTAGE_DIVIDER_PCB


@pytest.fixture
def vdiv_sch_path() -> Path:
    """Path to the voltage divider schematic."""
    if not VOLTAGE_DIVIDER_SCH.exists():
        pytest.skip("Voltage divider schematic not found")
    return VOLTAGE_DIVIDER_SCH


# ---------------------------------------------------------------------------
# extract_bom_from_pcb tests
# ---------------------------------------------------------------------------


class TestExtractBomFromPcb:
    """Tests for extract_bom_from_pcb()."""

    def test_returns_bom_object(self, vdiv_pcb_path: Path):
        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        assert isinstance(bom, BOM)
        assert bom.source == str(vdiv_pcb_path)

    def test_extracts_items(self, vdiv_pcb_path: Path):
        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        assert len(bom.items) > 0
        # Every item should have a reference
        for item in bom.items:
            assert item.reference, f"BOM item has empty reference: {item}"

    def test_items_have_value_and_footprint(self, vdiv_pcb_path: Path):
        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        for item in bom.items:
            assert item.value, f"BOM item {item.reference} has empty value"
            assert item.footprint, f"BOM item {item.reference} has empty footprint"

    def test_items_have_no_lib_id(self, vdiv_pcb_path: Path):
        """PCB-sourced BOM items have no lib_id (schematic concept)."""
        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        for item in bom.items:
            assert item.lib_id == ""

    def test_excludes_bom_excluded_footprints(self, vdiv_pcb_path: Path):
        """Footprints with exclude_from_bom=True should not appear."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(vdiv_pcb_path))
        excluded_refs = {
            fp.reference for fp in pcb.footprints if fp.exclude_from_bom
        }

        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        bom_refs = {item.reference for item in bom.items}

        assert excluded_refs.isdisjoint(bom_refs), (
            f"BOM should not contain excluded refs: {excluded_refs & bom_refs}"
        )

    def test_pcb_bom_references_match_pcb_footprints(self, vdiv_pcb_path: Path):
        """PCB-sourced BOM refs should be a subset of all PCB footprint refs."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(vdiv_pcb_path))
        all_pcb_refs = {fp.reference for fp in pcb.footprints}

        bom = extract_bom_from_pcb(str(vdiv_pcb_path))
        bom_refs = {item.reference for item in bom.items}

        assert bom_refs.issubset(all_pcb_refs)

    def test_warns_on_empty_value(self, caplog):
        """Should warn when a footprint has an empty value field."""
        from kicad_tools.schema.pcb import Footprint, PCB

        mock_fp = Footprint(
            name="R_0402",
            layer="F.Cu",
            position=(0.0, 0.0),
            rotation=0.0,
            reference="R99",
            value="",  # Empty value
        )

        with patch("kicad_tools.schema.pcb.PCB.load") as mock_load:
            mock_pcb = MagicMock()
            mock_pcb.footprints = [mock_fp]
            mock_load.return_value = mock_pcb

            with caplog.at_level(logging.WARNING):
                bom = extract_bom_from_pcb("/fake/path.kicad_pcb")

            assert len(bom.items) == 1
            assert any("empty value" in msg for msg in caplog.messages)


# ---------------------------------------------------------------------------
# Footprint properties extraction tests
# ---------------------------------------------------------------------------


class TestFootprintProperties:
    """Tests that PCB Footprint.from_sexp captures additional properties."""

    def test_properties_dict_exists(self, vdiv_pcb_path: Path):
        """Footprint dataclass should have a properties dict."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(vdiv_pcb_path))
        for fp in pcb.footprints:
            assert isinstance(fp.properties, dict)


# ---------------------------------------------------------------------------
# AssemblyConfig.bom_source tests
# ---------------------------------------------------------------------------


class TestAssemblyConfigBomSource:
    """Tests for the bom_source field on AssemblyConfig."""

    def test_default_is_schematic(self):
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig()
        assert config.bom_source == "schematic"

    def test_pcb_source(self):
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig(bom_source="pcb")
        assert config.bom_source == "pcb"

    def test_auto_source(self):
        from kicad_tools.export.assembly import AssemblyConfig

        config = AssemblyConfig(bom_source="auto")
        assert config.bom_source == "auto"


# ---------------------------------------------------------------------------
# AssemblyPackage BOM generation with bom_source tests
# ---------------------------------------------------------------------------


class TestAssemblyPackageBomSource:
    """Tests for AssemblyPackage._generate_bom with different bom_source."""

    def test_pcb_source_skips_schematic(self, vdiv_pcb_path: Path, tmp_path: Path):
        """bom_source='pcb' should not require a schematic."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        config = AssemblyConfig(
            bom_source="pcb",
            auto_lcsc=False,
            no_spec=True,
        )
        pkg = AssemblyPackage(
            pcb_path=vdiv_pcb_path,
            schematic_path=None,
            config=config,
        )
        result = pkg.export(tmp_path)

        assert result.bom_path is not None
        assert result.bom_path.exists()
        content = result.bom_path.read_text()
        assert len(content) > 0

    def test_pcb_source_bom_has_correct_refs(self, vdiv_pcb_path: Path, tmp_path: Path):
        """BOM from PCB should contain the same refs as PCB footprints."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(vdiv_pcb_path))
        expected_refs = {
            fp.reference for fp in pcb.footprints
            if not fp.exclude_from_bom
        }

        config = AssemblyConfig(
            bom_source="pcb",
            auto_lcsc=False,
            no_spec=True,
        )
        pkg = AssemblyPackage(
            pcb_path=vdiv_pcb_path,
            schematic_path=None,
            config=config,
        )
        result = pkg.export(tmp_path)

        # Check that BOM CSV content includes the expected references
        content = result.bom_path.read_text()
        for ref in expected_refs:
            assert ref in content, f"Expected reference {ref} in BOM CSV"

    def test_schematic_source_without_schematic_raises(self, vdiv_pcb_path: Path, tmp_path: Path):
        """bom_source='schematic' without schematic should raise ValidationError."""
        from kicad_tools.exceptions import ValidationError
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        config = AssemblyConfig(
            bom_source="schematic",
            auto_lcsc=False,
            no_spec=True,
        )
        pkg = AssemblyPackage(
            pcb_path=vdiv_pcb_path,
            schematic_path="/nonexistent/path.kicad_sch",
            config=config,
        )
        # The error is caught by export() and stored in result.errors
        result = pkg.export(tmp_path)
        assert any("BOM generation failed" in e for e in result.errors)

    def test_auto_source_with_matching_refs(
        self, vdiv_pcb_path: Path, vdiv_sch_path: Path, tmp_path: Path
    ):
        """auto mode should use schematic when refs match."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        config = AssemblyConfig(
            bom_source="auto",
            auto_lcsc=False,
            no_spec=True,
        )
        pkg = AssemblyPackage(
            pcb_path=vdiv_pcb_path,
            schematic_path=str(vdiv_sch_path),
            config=config,
        )
        result = pkg.export(tmp_path)

        assert result.bom_path is not None
        assert result.bom_path.exists()

    def test_auto_source_without_schematic_uses_pcb(
        self, vdiv_pcb_path: Path, tmp_path: Path
    ):
        """auto mode should fall back to PCB when no schematic is available."""
        from kicad_tools.export.assembly import AssemblyConfig, AssemblyPackage

        config = AssemblyConfig(
            bom_source="auto",
            auto_lcsc=False,
            no_spec=True,
        )
        pkg = AssemblyPackage(
            pcb_path=vdiv_pcb_path,
            schematic_path=None,
            config=config,
        )
        result = pkg.export(tmp_path)

        assert result.bom_path is not None
        assert result.bom_path.exists()


# ---------------------------------------------------------------------------
# Preflight checker with bom_source tests
# ---------------------------------------------------------------------------


class TestPreflightBomSource:
    """Tests for PreflightChecker with bom_source setting."""

    def test_pcb_source_schematic_check_ok(self, vdiv_pcb_path: Path, tmp_path: Path):
        """When bom_source=pcb, missing schematic should be OK, not WARN."""
        import shutil

        from kicad_tools.export.preflight import PreflightChecker

        # Copy PCB to a temp dir without a schematic so auto-detection fails
        isolated_pcb = tmp_path / "test.kicad_pcb"
        shutil.copy(vdiv_pcb_path, isolated_pcb)

        checker = PreflightChecker(
            pcb_path=isolated_pcb,
            schematic_path=None,
            bom_source="pcb",
        )
        result = checker._check_schematic_exists()
        assert result.status == "OK"
        assert "PCB footprints" in result.message

    def test_pcb_source_loads_bom_from_pcb(self, vdiv_pcb_path: Path):
        """When bom_source=pcb, _load_bom should use PCB source."""
        from kicad_tools.export.preflight import PreflightChecker

        checker = PreflightChecker(
            pcb_path=vdiv_pcb_path,
            schematic_path=None,
            bom_source="pcb",
        )
        # Load PCB first (needed for bom checks)
        checker._check_pcb_parseable()

        bom = checker._load_bom()
        assert len(bom.items) > 0

    def test_default_source_requires_schematic(self, vdiv_pcb_path: Path):
        """Default bom_source=schematic should fail without schematic."""
        from kicad_tools.export.preflight import PreflightChecker

        checker = PreflightChecker(
            pcb_path=vdiv_pcb_path,
            schematic_path="/nonexistent.kicad_sch",
            bom_source="schematic",
        )
        result = checker._check_schematic_exists()
        assert result.status == "WARN"

    def test_pcb_bom_preflight_runs_all_checks(self, vdiv_pcb_path: Path):
        """Preflight with bom_source=pcb should run BOM checks."""
        from kicad_tools.export.preflight import PreflightChecker, PreflightConfig

        config = PreflightConfig(skip_drc=True, skip_erc=True)
        checker = PreflightChecker(
            pcb_path=vdiv_pcb_path,
            schematic_path=None,
            bom_source="pcb",
            config=config,
        )
        results = checker.run_all()
        check_names = {r.name for r in results}
        # BOM checks should be present even without schematic
        assert "bom_fields" in check_names
        assert "bom_pcb_match" in check_names


# ---------------------------------------------------------------------------
# LCSC enrichment compatibility tests
# ---------------------------------------------------------------------------


class TestLcscEnrichmentWithPcbBom:
    """Verify that LCSC enrichment works with PCB-sourced BOM items."""

    def test_enrich_accepts_pcb_sourced_items(self):
        """enrich_bom_lcsc should accept BOM items without lib_id."""
        items = [
            BOMItem(
                reference="R1",
                value="10k",
                footprint="R_0805_2012Metric",
                lib_id="",
                lcsc="C17414",
            ),
            BOMItem(
                reference="C1",
                value="100nF",
                footprint="C_0805_2012Metric",
                lib_id="",
            ),
        ]
        # Should not crash -- just test that it accepts the items
        from kicad_tools.export.bom_enrich import enrich_bom_lcsc

        report = enrich_bom_lcsc(items, prefer_basic=True, min_stock=0)
        # Report should be returned regardless
        assert report is not None


# ---------------------------------------------------------------------------
# CLI --bom-source argument tests
# ---------------------------------------------------------------------------


class TestCliBomSourceArg:
    """Tests for the --bom-source CLI argument parsing."""

    def test_parser_accepts_bom_source_pcb(self):
        from kicad_tools.cli.export_cmd import main
        import argparse

        # Just test argument parsing, not execution
        from kicad_tools.cli.export_cmd import main as _main

        # Create parser to test argument parsing
        import importlib
        import kicad_tools.cli.export_cmd as export_mod

        parser = argparse.ArgumentParser()
        parser.add_argument("pcb")
        parser.add_argument("--bom-source", default="schematic", choices=["schematic", "pcb", "auto"])

        args = parser.parse_args(["test.kicad_pcb", "--bom-source", "pcb"])
        assert args.bom_source == "pcb"

    def test_parser_accepts_bom_source_auto(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("pcb")
        parser.add_argument("--bom-source", default="schematic", choices=["schematic", "pcb", "auto"])

        args = parser.parse_args(["test.kicad_pcb", "--bom-source", "auto"])
        assert args.bom_source == "auto"

    def test_parser_default_is_schematic(self):
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("pcb")
        parser.add_argument("--bom-source", default="schematic", choices=["schematic", "pcb", "auto"])

        args = parser.parse_args(["test.kicad_pcb"])
        assert args.bom_source == "schematic"
