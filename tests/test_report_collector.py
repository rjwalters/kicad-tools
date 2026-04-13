"""Tests for report data collector.

Tests cover:
- Board summary collection (layers, footprints, nets, traces, vias, dimensions)
- DRC collection from AuditResult
- BOM collection from schematic
- Net status collection
- Analysis collection (congestion, SI, thermal)
- Fault tolerance (sub-collector failures produce null, not exceptions)
- Schema versioning (schema_version + generated_at in every JSON file)
- Integration: collect_all writes files and produces valid JSON
- PCB-only mode (no schematic): BOM skipped gracefully
- Empty PCB produces valid zeroed snapshots
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.report.collector import SCHEMA_VERSION, ReportDataCollector

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_FIXTURES = FIXTURES / "projects"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_audit_result():
    """Create a mock AuditResult with known DRC counts."""
    from kicad_tools.audit.auditor import (
        ActionItem,
        AuditResult,
        ConnectivityStatus,
        CostEstimate,
        DRCStatus,
        ERCStatus,
        LayerUtilization,
        ManufacturerCompatibility,
    )

    return AuditResult(
        project_name="test",
        drc=DRCStatus(
            error_count=2,
            warning_count=3,
            blocking_count=1,
            passed=False,
            details="clearance (2)",
        ),
        erc=ERCStatus(error_count=0, warning_count=1, passed=True),
        connectivity=ConnectivityStatus(
            total_nets=10,
            connected_nets=8,
            incomplete_nets=2,
            completion_percent=80.0,
            unconnected_pads=4,
            passed=False,
        ),
        compatibility=ManufacturerCompatibility(manufacturer="JLCPCB", passed=True),
        layers=LayerUtilization(layer_count=2),
        cost=CostEstimate(pcb_cost=5.0, total_cost=5.0, quantity=5),
        action_items=[ActionItem(priority=1, description="Fix clearance")],
    )


# ---------------------------------------------------------------------------
# Unit tests - board summary
# ---------------------------------------------------------------------------


class TestCollectBoardSummary:
    """Tests for collect_board_summary."""

    def test_returns_expected_keys(self):
        """Board summary contains all required fields."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        summary = collector.collect_board_summary(pcb)

        expected_keys = {
            "layer_count",
            "layer_names",
            "footprint_count",
            "footprint_smd",
            "footprint_tht",
            "footprint_other",
            "net_count",
            "segment_count",
            "via_count",
            "board_width_mm",
            "board_height_mm",
        }
        assert expected_keys == set(summary.keys())

    def test_layer_count_is_positive(self):
        """Board must have at least one copper layer."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        summary = collector.collect_board_summary(pcb)

        assert summary["layer_count"] >= 1
        assert len(summary["layer_names"]) == summary["layer_count"]

    def test_footprint_breakdown_sums(self):
        """SMD + THT + other should equal total."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        summary = collector.collect_board_summary(pcb)

        total = summary["footprint_smd"] + summary["footprint_tht"] + summary["footprint_other"]
        assert total == summary["footprint_count"]


# ---------------------------------------------------------------------------
# Unit tests - DRC collect
# ---------------------------------------------------------------------------


class TestCollectDRC:
    """Tests for collect_drc."""

    def test_extracts_drc_from_audit_result(self):
        """DRC data matches the AuditResult's DRC fields."""
        audit_result = _make_mock_audit_result()
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        drc = collector.collect_drc(audit_result)

        assert drc is not None
        assert drc["error_count"] == 2
        assert drc["warning_count"] == 3
        assert drc["blocking_count"] == 1
        assert drc["passed"] is False

    def test_returns_none_when_no_audit(self):
        """collect_drc returns None when audit_result is None."""
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        assert collector.collect_drc(None) is None


# ---------------------------------------------------------------------------
# Unit tests - BOM collect
# ---------------------------------------------------------------------------


class TestCollectBOM:
    """Tests for collect_bom."""

    def test_collects_bom_from_schematic(self):
        """BOM returns grouped items with quantity and lcsc fields."""
        sch_path = PROJECT_FIXTURES / "test_project.kicad_sch"
        if not sch_path.exists():
            pytest.skip("test_project.kicad_sch fixture not found")

        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        bom_data = collector.collect_bom(sch_path)

        assert "total_components" in bom_data
        assert "unique_parts" in bom_data
        assert "dnp_count" in bom_data
        assert "groups" in bom_data
        assert isinstance(bom_data["groups"], list)

        # Each group should have quantity and lcsc fields
        for group in bom_data["groups"]:
            assert "quantity" in group
            assert "lcsc" in group
            assert "references" in group
            assert "value" in group
            assert "footprint" in group


# ---------------------------------------------------------------------------
# Unit tests - net status collect
# ---------------------------------------------------------------------------


class TestCollectNetStatus:
    """Tests for collect_net_status."""

    def test_returns_expected_keys(self):
        """Net status returns total_nets, complete_count, completion_pct."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        status = collector.collect_net_status(pcb)

        assert "total_nets" in status
        assert "complete_count" in status
        assert "incomplete_count" in status
        assert "unrouted_count" in status
        assert "total_unconnected_pads" in status
        assert "completion_pct" in status

    def test_completion_pct_range(self):
        """Completion percentage is between 0 and 100."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        status = collector.collect_net_status(pcb)

        assert 0.0 <= status["completion_pct"] <= 100.0


# ---------------------------------------------------------------------------
# Unit tests - fault tolerance
# ---------------------------------------------------------------------------


class TestFaultTolerance:
    """Tests for fault tolerance in collect_all and collect_analysis."""

    def test_congestion_failure_produces_null(self):
        """If CongestionAnalyzer.analyze raises, congestion is None."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.congestion.CongestionAnalyzer.analyze",
            side_effect=RuntimeError("boom"),
        ):
            result = collector.collect_analysis(pcb)

        assert result["congestion"] is None
        # Other sections should still be populated
        assert result["signal_integrity"] is not None
        assert result["thermal"] is not None

    def test_si_failure_produces_null(self):
        """If SignalIntegrityAnalyzer.analyze_crosstalk raises, SI is None."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.signal_integrity.SignalIntegrityAnalyzer.analyze_crosstalk",
            side_effect=RuntimeError("boom"),
        ):
            result = collector.collect_analysis(pcb)

        assert result["signal_integrity"] is None

    def test_thermal_failure_produces_null(self):
        """If ThermalAnalyzer.analyze raises, thermal is None."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.thermal.ThermalAnalyzer.analyze",
            side_effect=RuntimeError("boom"),
        ):
            result = collector.collect_analysis(pcb)

        assert result["thermal"] is None

    def test_collect_all_survives_sub_collector_failure(self, tmp_path):
        """collect_all completes even when one sub-collector raises."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        collector = ReportDataCollector(pcb_path, skip_erc=True)

        with patch(
            "kicad_tools.analysis.congestion.CongestionAnalyzer.analyze",
            side_effect=RuntimeError("boom"),
        ):
            files = collector.collect_all(tmp_path)

        # Should still produce files for all categories
        assert "board_summary" in files
        assert "analysis" in files

        # analysis.json should exist but congestion inside should be null
        with open(files["analysis"]) as f:
            data = json.load(f)
        assert data["data"]["congestion"] is None


# ---------------------------------------------------------------------------
# Unit tests - schema versioning
# ---------------------------------------------------------------------------


class TestSchemaVersioning:
    """Tests for schema version and timestamp in every JSON file."""

    def test_all_files_have_schema_version(self, tmp_path):
        """Every written JSON file has schema_version and generated_at."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        files = collector.collect_all(tmp_path)

        for name, fpath in files.items():
            with open(fpath) as f:
                data = json.load(f)
            assert data["schema_version"] == SCHEMA_VERSION, f"{name} missing schema_version"
            assert "generated_at" in data, f"{name} missing generated_at"
            assert "pcb_path" in data, f"{name} missing pcb_path"


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestCollectAllIntegration:
    """Integration tests for collect_all."""

    def test_writes_expected_files(self, tmp_path):
        """collect_all writes all expected JSON files to output dir."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        files = collector.collect_all(tmp_path)

        expected_names = {"board_summary", "drc_summary", "audit", "net_status", "analysis"}
        # BOM is optional (depends on schematic existing)
        for name in expected_names:
            assert name in files, f"Missing file: {name}"
            assert files[name].exists(), f"File not on disk: {name}"

    def test_json_validity(self, tmp_path):
        """All written files are valid JSON."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        files = collector.collect_all(tmp_path)

        for name, fpath in files.items():
            with open(fpath) as f:
                data = json.load(f)  # Should not raise
            assert isinstance(data, dict), f"{name} is not a dict"

    def test_pcb_only_no_schematic(self, tmp_path):
        """collect_all with no schematic skips BOM gracefully."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        # Use a PCB path where no schematic exists alongside it
        # Create a temp PCB by copying
        import shutil

        isolated_pcb = tmp_path / "isolated" / "board.kicad_pcb"
        isolated_pcb.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(pcb_path, isolated_pcb)

        output_dir = tmp_path / "output"
        collector = ReportDataCollector(isolated_pcb, skip_erc=True)
        files = collector.collect_all(output_dir)

        # BOM should be absent (no schematic)
        assert "bom" not in files

        # Other files should exist
        assert "board_summary" in files
        assert "net_status" in files
        assert "analysis" in files


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge case tests."""

    def test_empty_pcb_produces_valid_snapshots(self, tmp_path):
        """A PCB with no footprints or nets produces valid zeroed snapshots."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=50.0, height=50.0, layers=2)

        # Save PCB to disk so the collector can reference it
        pcb_path = tmp_path / "empty.kicad_pcb"
        pcb.save(str(pcb_path))

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        summary = collector.collect_board_summary(pcb)

        assert summary["footprint_count"] == 0
        assert summary["net_count"] >= 0
        assert summary["segment_count"] == 0
        assert summary["via_count"] == 0

    def test_empty_pcb_net_status(self, tmp_path):
        """Net status on empty PCB produces valid output."""
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.create(width=50.0, height=50.0, layers=2)
        pcb_path = tmp_path / "empty.kicad_pcb"
        pcb.save(str(pcb_path))

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        status = collector.collect_net_status(pcb)

        assert status["total_nets"] >= 0
        assert status["completion_pct"] >= 0.0


# ---------------------------------------------------------------------------
# BOMItem / BOMGroup to_dict tests
# ---------------------------------------------------------------------------


class TestBOMSerialization:
    """Tests for BOMItem.to_dict and BOMGroup.to_dict."""

    def test_bom_item_to_dict(self):
        """BOMItem.to_dict returns expected keys."""
        from kicad_tools.schema.bom import BOMItem

        item = BOMItem(
            reference="R1",
            value="10k",
            footprint="Resistor_SMD:R_0402_1005Metric",
            lib_id="Device:R",
            description="Resistor",
            manufacturer="Yageo",
            mpn="RC0402FR-0710KL",
            lcsc="C25744",
        )
        d = item.to_dict()
        assert d["reference"] == "R1"
        assert d["value"] == "10k"
        assert d["lcsc"] == "C25744"
        assert d["mpn"] == "RC0402FR-0710KL"
        assert d["dnp"] is False

    def test_bom_group_to_dict(self):
        """BOMGroup.to_dict returns quantity, references, lcsc."""
        from kicad_tools.schema.bom import BOMGroup, BOMItem

        items = [
            BOMItem(
                reference="R1",
                value="10k",
                footprint="R_0402",
                lib_id="Device:R",
                lcsc="C25744",
            ),
            BOMItem(
                reference="R2",
                value="10k",
                footprint="R_0402",
                lib_id="Device:R",
                lcsc="C25744",
            ),
        ]
        group = BOMGroup(value="10k", footprint="R_0402", items=items)
        d = group.to_dict()

        assert d["quantity"] == 2
        assert "R1" in d["references"]
        assert "R2" in d["references"]
        assert d["lcsc"] == "C25744"
        assert len(d["items"]) == 2
