"""Tests for report data collector.

Tests cover:
- Board summary collection (layers, footprints, nets, traces, vias, dimensions)
- DRC collection from AuditResult
- Cost collection from AuditResult (field normalisation for template)
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
            "declared_net_count",
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
# Unit tests - ERC collect
# ---------------------------------------------------------------------------


class TestCollectERC:
    """Tests for collect_erc."""

    def test_extracts_erc_from_audit_result(self):
        """ERC data matches the AuditResult's ERC fields."""
        audit_result = _make_mock_audit_result()
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        erc = collector.collect_erc(audit_result)

        assert erc is not None
        assert erc["error_count"] == 0
        assert erc["warning_count"] == 1
        assert erc["passed"] is True

    def test_returns_none_when_no_audit(self):
        """collect_erc returns None when audit_result is None."""
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        assert collector.collect_erc(None) is None

    def test_erc_with_errors(self):
        """ERC data reflects error counts from AuditResult."""
        from kicad_tools.audit.auditor import (
            AuditResult,
            ConnectivityStatus,
            CostEstimate,
            DRCStatus,
            ERCStatus,
            LayerUtilization,
            ManufacturerCompatibility,
        )

        audit_result = AuditResult(
            project_name="test_erc",
            drc=DRCStatus(error_count=0, warning_count=0, blocking_count=0, passed=True),
            erc=ERCStatus(
                error_count=3,
                warning_count=1,
                passed=False,
                details="pin_not_connected (2x), power_pin_not_driven (1x)",
            ),
            connectivity=ConnectivityStatus(
                total_nets=10,
                connected_nets=10,
                incomplete_nets=0,
                completion_percent=100.0,
                unconnected_pads=0,
                passed=True,
            ),
            compatibility=ManufacturerCompatibility(manufacturer="JLCPCB", passed=True),
            layers=LayerUtilization(layer_count=2),
            cost=CostEstimate(pcb_cost=5.0, total_cost=5.0, quantity=5),
            action_items=[],
        )
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        erc = collector.collect_erc(audit_result)

        assert erc is not None
        assert erc["error_count"] == 3
        assert erc["warning_count"] == 1
        assert erc["passed"] is False
        assert "pin_not_connected" in erc["details"]


# ---------------------------------------------------------------------------
# Unit tests - cost collect
# ---------------------------------------------------------------------------


class TestCollectCost:
    """Tests for collect_cost."""

    def test_collect_cost_from_audit_result(self):
        """Cost data has all template-expected keys including pcb_cost."""
        audit_result = _make_mock_audit_result()
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        assert cost is not None
        expected_keys = {
            "pcb_cost",
            "component_cost",
            "assembly_cost",
            "total",
            "per_unit",
            "batch_qty",
            "batch_total",
            "currency",
        }
        assert expected_keys == set(cost.keys())
        # mock: total_cost=5.0, quantity=5 => per_unit=1.0
        assert cost["per_unit"] == 1.0
        assert cost["batch_qty"] == 5
        assert cost["batch_total"] == 5.0
        assert cost["currency"] == "USD"
        # pcb_cost and total should also be present
        assert cost["pcb_cost"] == 1.0  # 5.0 / 5
        assert cost["total"] == 1.0

    def test_collect_cost_returns_none_when_no_audit(self):
        """collect_cost returns None when audit_result is None."""
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        assert collector.collect_cost(None) is None

    def test_collect_cost_quantity_zero_no_division_error(self):
        """quantity=0 produces per_unit=0.0 instead of ZeroDivisionError."""
        from kicad_tools.audit.auditor import AuditResult, CostEstimate

        audit_result = AuditResult(
            project_name="test",
            cost=CostEstimate(pcb_cost=10.0, total_cost=10.0, quantity=0),
        )
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        assert cost is not None
        assert cost["per_unit"] == 0.0
        assert cost["pcb_cost"] == 0.0
        assert cost["batch_qty"] == 0
        assert cost["batch_total"] == 10.0

    def test_collect_cost_rounds_per_unit(self):
        """per_unit is rounded to 2 decimal places."""
        from kicad_tools.audit.auditor import AuditResult, CostEstimate

        audit_result = AuditResult(
            project_name="test",
            cost=CostEstimate(pcb_cost=10.0, total_cost=10.0, quantity=3),
        )
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        # 10.0 / 3 = 3.333... -> rounded to 3.33
        assert cost["per_unit"] == 3.33

    def test_collect_cost_pcb_only_no_component_data(self):
        """component_cost is None when CostEstimate.component_cost is None."""
        from kicad_tools.audit.auditor import AuditResult, CostEstimate

        audit_result = AuditResult(
            project_name="test",
            cost=CostEstimate(
                pcb_cost=8.0,
                component_cost=None,
                assembly_cost=None,
                total_cost=8.0,
                quantity=4,
            ),
        )
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        assert cost is not None
        assert cost["pcb_cost"] == 2.0  # 8.0 / 4
        assert cost["component_cost"] is None
        assert cost["assembly_cost"] is None
        assert cost["total"] == 2.0  # 8.0 / 4

    def test_collect_cost_with_component_and_assembly(self):
        """All three cost sub-groups are populated when data is available."""
        from kicad_tools.audit.auditor import AuditResult, CostEstimate

        audit_result = AuditResult(
            project_name="test",
            cost=CostEstimate(
                pcb_cost=10.0,
                component_cost=6.0,
                assembly_cost=4.0,
                total_cost=20.0,
                quantity=5,
            ),
        )
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        assert cost is not None
        assert cost["pcb_cost"] == 2.0  # 10.0 / 5
        assert cost["component_cost"] == 1.2  # 6.0 / 5
        assert cost["assembly_cost"] == 0.8  # 4.0 / 5
        assert cost["total"] == 4.0  # 20.0 / 5
        assert cost["batch_total"] == 20.0


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

        # Each group should have qty and lcsc fields
        for group in bom_data["groups"]:
            assert "qty" in group
            assert "lcsc" in group
            assert "refs" in group
            assert "value" in group
            assert "footprint" in group


# ---------------------------------------------------------------------------
# Unit tests - net status collect
# ---------------------------------------------------------------------------


class TestCollectNetStatus:
    """Tests for collect_net_status."""

    def test_returns_expected_keys(self):
        """Net status returns total_nets, complete_count, completion_percent, etc."""
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
        assert "completion_percent" in status
        assert "incomplete_net_names" in status
        # The old key must not appear
        assert "completion_pct" not in status

    def test_completion_percent_range(self):
        """Completion percentage is between 0 and 100."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        status = collector.collect_net_status(pcb)

        assert 0.0 <= status["completion_percent"] <= 100.0

    def test_incomplete_net_names_is_sorted_list(self):
        """incomplete_net_names is a sorted list of strings."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        status = collector.collect_net_status(pcb)

        names = status["incomplete_net_names"]
        assert isinstance(names, list)
        # All entries are strings
        assert all(isinstance(n, str) for n in names)
        # List is sorted
        assert names == sorted(names)


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
        """If TraceIntegrityAnalyzer.analyze_crosstalk raises, SI is None."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.signal_integrity.TraceIntegrityAnalyzer.analyze_crosstalk",
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

        expected_names = {
            "board_summary",
            "drc_summary",
            "erc_summary",
            "audit",
            "cost",
            "net_status",
            "analysis",
        }
        # BOM is optional (depends on schematic existing)
        for name in expected_names:
            assert name in files, f"Missing file: {name}"
            assert files[name].exists(), f"File not on disk: {name}"

    def test_cost_json_has_normalised_keys(self, tmp_path):
        """cost.json contains pcb_cost, component_cost, total, and legacy keys."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        if not pcb_path.exists():
            pytest.skip("test_project.kicad_pcb fixture not found")

        collector = ReportDataCollector(pcb_path, skip_erc=True)
        files = collector.collect_all(tmp_path)

        assert "cost" in files, "cost.json not written by collect_all"
        with open(files["cost"]) as f:
            envelope = json.load(f)

        cost_data = envelope["data"]
        # cost_data may be None if the cost estimator returned defaults,
        # but the dict itself should be present and have the right shape.
        assert cost_data is not None, "cost data should not be null for a valid audit"
        # New breakdown keys
        assert "pcb_cost" in cost_data
        assert "component_cost" in cost_data  # may be None
        assert "assembly_cost" in cost_data  # may be None
        assert "total" in cost_data
        # Legacy keys preserved
        assert "per_unit" in cost_data
        assert "batch_qty" in cost_data
        assert "batch_total" in cost_data
        assert "currency" in cost_data

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
        assert status["completion_percent"] >= 0.0
        assert status["incomplete_net_names"] == []


# ---------------------------------------------------------------------------
# Net status scenarios (mocked NetStatusAnalyzer)
# ---------------------------------------------------------------------------


def _make_net_status_result(complete_names, incomplete_names, unrouted_names):
    """Build a mock NetStatusResult with the given net name lists."""
    from kicad_tools.analysis.net_status import NetStatus, NetStatusResult

    nets = []
    for name in complete_names:
        ns = NetStatus(net_number=len(nets) + 1, net_name=name, total_pads=2)
        ns.connected_pads = [object(), object()]  # two connected pads
        nets.append(ns)
    for name in incomplete_names:
        ns = NetStatus(net_number=len(nets) + 1, net_name=name, total_pads=3)
        ns.connected_pads = [object()]  # one connected
        ns.unconnected_pads = [object(), object()]  # two unconnected
        nets.append(ns)
    for name in unrouted_names:
        ns = NetStatus(net_number=len(nets) + 1, net_name=name, total_pads=2)
        # No connected pads, two unconnected
        ns.unconnected_pads = [object(), object()]
        nets.append(ns)

    result = NetStatusResult(nets=nets, total_nets=len(nets))
    return result


class TestCollectNetStatusScenarios:
    """Tests for collect_net_status with controlled scenarios."""

    def test_incomplete_only_scenario(self, tmp_path):
        """Board with incomplete nets and 0 unrouted shows correct counts."""
        mock_result = _make_net_status_result(
            complete_names=["GND", "VCC"],
            incomplete_names=["SDA", "SCL", "MOSI"],
            unrouted_names=[],
        )
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["incomplete_count"] == 3
        assert status["unrouted_count"] == 0
        assert status["complete_count"] == 2
        assert status["total_nets"] == 5
        assert status["completion_percent"] == 40.0  # 2/5 = 40%
        assert status["incomplete_net_names"] == ["MOSI", "SCL", "SDA"]

    def test_mixed_incomplete_and_unrouted(self, tmp_path):
        """Board with both incomplete and unrouted nets."""
        mock_result = _make_net_status_result(
            complete_names=["GND"],
            incomplete_names=["SDA", "SCL"],
            unrouted_names=["RESET"],
        )
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["incomplete_count"] == 2
        assert status["unrouted_count"] == 1
        assert status["complete_count"] == 1
        # incomplete_net_names includes both incomplete and unrouted
        assert status["incomplete_net_names"] == ["RESET", "SCL", "SDA"]

    def test_fully_routed_scenario(self, tmp_path):
        """Board with all nets complete produces empty incomplete_net_names."""
        mock_result = _make_net_status_result(
            complete_names=["GND", "VCC", "+3V3"],
            incomplete_names=[],
            unrouted_names=[],
        )
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["incomplete_count"] == 0
        assert status["unrouted_count"] == 0
        assert status["complete_count"] == 3
        assert status["completion_percent"] == 100.0
        assert status["incomplete_net_names"] == []

    def test_incomplete_net_names_capped(self, tmp_path):
        """incomplete_net_names is capped at _INCOMPLETE_NET_NAMES_CAP."""
        # Create more incomplete nets than the cap
        incomplete_names = [f"NET_{i:03d}" for i in range(70)]
        mock_result = _make_net_status_result(
            complete_names=[],
            incomplete_names=incomplete_names,
            unrouted_names=[],
        )
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert len(status["incomplete_net_names"]) == 50
        # Should be the first 50 when sorted alphabetically
        assert status["incomplete_net_names"] == sorted(incomplete_names)[:50]


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

        assert d["qty"] == 2
        assert "R1" in d["refs"]
        assert "R2" in d["refs"]
        assert d["lcsc"] == "C25744"
        assert len(d["items"]) == 2


# ---------------------------------------------------------------------------
# Net classification in collect_net_status
# ---------------------------------------------------------------------------


class TestCollectNetStatusClassification:
    """Tests for net type classification (signal/zone/single-pad) in collect_net_status."""

    def _make_classified_result(self):
        """Build a mock NetStatusResult with zone, single-pad, and signal nets."""
        from kicad_tools.analysis.net_status import NetStatus, NetStatusResult

        nets = []

        # Zone-connected net (plane net, incomplete by trace but connected by zone)
        gnd = NetStatus(net_number=1, net_name="GND", total_pads=10)
        gnd.is_plane_net = True
        gnd.plane_layer = "B.Cu"
        gnd.connected_pads = [object()] * 5
        gnd.unconnected_pads = [object()] * 5  # incomplete by trace
        nets.append(gnd)

        # Zone-connected net (complete)
        vcc = NetStatus(net_number=2, net_name="+3.3V", total_pads=6)
        vcc.is_plane_net = True
        vcc.plane_layer = "F.Cu"
        vcc.connected_pads = [object()] * 6
        nets.append(vcc)

        # Single-pad net
        nss = NetStatus(net_number=3, net_name="SPI_NSS", total_pads=1)
        nss.connected_pads = [object()]
        nets.append(nss)

        # Signal net (complete)
        sda = NetStatus(net_number=4, net_name="SDA", total_pads=2)
        sda.connected_pads = [object(), object()]
        nets.append(sda)

        # Signal net (complete)
        scl = NetStatus(net_number=5, net_name="SCL", total_pads=2)
        scl.connected_pads = [object(), object()]
        nets.append(scl)

        # Signal net (incomplete)
        miso = NetStatus(net_number=6, net_name="MISO", total_pads=3)
        miso.connected_pads = [object()]
        miso.unconnected_pads = [object(), object()]
        nets.append(miso)

        return NetStatusResult(nets=nets, total_nets=6)

    def test_new_keys_present(self, tmp_path):
        """collect_net_status returns all new classification keys."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        new_keys = {
            "signal_net_count",
            "signal_complete_count",
            "signal_completion_percent",
            "signal_incomplete_net_names",
            "zone_connected_count",
            "zone_connected_nets",
            "single_pad_count",
            "single_pad_nets",
        }
        for key in new_keys:
            assert key in status, f"Missing key: {key}"

    def test_signal_completion_excludes_zone_and_single_pad(self, tmp_path):
        """Signal completion percentage excludes zone-connected and single-pad nets."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        # Signal nets: SDA (complete), SCL (complete), MISO (incomplete) = 3 total
        assert status["signal_net_count"] == 3
        assert status["signal_complete_count"] == 2
        # 2/3 = 66.7%
        assert status["signal_completion_percent"] == 66.7

    def test_zone_connected_nets_identified(self, tmp_path):
        """Zone-connected nets include all plane nets."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["zone_connected_count"] == 2
        assert "+3.3V" in status["zone_connected_nets"]
        assert "GND" in status["zone_connected_nets"]

    def test_single_pad_nets_identified(self, tmp_path):
        """Single-pad nets are correctly identified."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["single_pad_count"] == 1
        assert "SPI_NSS" in status["single_pad_nets"]

    def test_signal_incomplete_excludes_zone_and_single_pad(self, tmp_path):
        """signal_incomplete_net_names excludes zone-connected and single-pad nets."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        # Only MISO is an incomplete signal net
        assert status["signal_incomplete_net_names"] == ["MISO"]
        # GND is incomplete but zone-connected, should not be in signal list
        assert "GND" not in status["signal_incomplete_net_names"]

    def test_backward_compat_keys_preserved(self, tmp_path):
        """Existing keys (total_nets, complete_count, etc.) are unchanged."""
        mock_result = self._make_classified_result()
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["total_nets"] == 6
        # complete: +3.3V, SPI_NSS (1 pad = complete), SDA, SCL = 4
        assert status["complete_count"] == 4
        # incomplete: GND, MISO = 2
        assert status["incomplete_count"] == 2
        assert status["unrouted_count"] == 0

    def test_signal_incomplete_named_excludes_auto_generated(self, tmp_path):
        """signal_incomplete_named excludes Net-(...) and unconnected-(...) nets."""
        from kicad_tools.analysis.net_status import NetStatus, NetStatusResult

        nets = []
        # Named signal net (incomplete)
        miso = NetStatus(net_number=1, net_name="MISO", total_pads=3)
        miso.connected_pads = [object()]
        miso.unconnected_pads = [object(), object()]
        nets.append(miso)

        # Auto-generated net (incomplete)
        auto1 = NetStatus(net_number=2, net_name="Net-(U1-1)", total_pads=2)
        auto1.connected_pads = [object()]
        auto1.unconnected_pads = [object()]
        nets.append(auto1)

        # Another auto-generated net (incomplete)
        auto2 = NetStatus(net_number=3, net_name="unconnected-(C40-2)", total_pads=2)
        auto2.connected_pads = [object()]
        auto2.unconnected_pads = [object()]
        nets.append(auto2)

        # Named signal net (incomplete)
        sda = NetStatus(net_number=4, net_name="SDA", total_pads=2)
        sda.connected_pads = [object()]
        sda.unconnected_pads = [object()]
        nets.append(sda)

        # Named signal net (complete)
        scl = NetStatus(net_number=5, net_name="SCL", total_pads=2)
        scl.connected_pads = [object(), object()]
        nets.append(scl)

        mock_result = NetStatusResult(nets=nets, total_nets=5)
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        # signal_incomplete_named should contain only named nets
        assert status["signal_incomplete_named"] == ["MISO", "SDA"]
        # signal_incomplete_auto_count should count the auto-generated ones
        assert status["signal_incomplete_auto_count"] == 2
        # signal_incomplete_net_names (backward compat) should contain all four
        assert status["signal_incomplete_net_names"] == [
            "MISO",
            "Net-(U1-1)",
            "SDA",
            "unconnected-(C40-2)",
        ]

    def test_signal_incomplete_all_auto_generated(self, tmp_path):
        """When all incomplete signal nets are auto-generated, named list is empty."""
        from kicad_tools.analysis.net_status import NetStatus, NetStatusResult

        nets = []
        auto1 = NetStatus(net_number=1, net_name="Net-(U1-1)", total_pads=2)
        auto1.connected_pads = [object()]
        auto1.unconnected_pads = [object()]
        nets.append(auto1)

        auto2 = NetStatus(net_number=2, net_name="Net-(R3-2)", total_pads=2)
        auto2.connected_pads = [object()]
        auto2.unconnected_pads = [object()]
        nets.append(auto2)

        mock_result = NetStatusResult(nets=nets, total_nets=2)
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["signal_incomplete_named"] == []
        assert status["signal_incomplete_auto_count"] == 2

    def test_signal_incomplete_all_named(self, tmp_path):
        """When all incomplete signal nets are named, auto count is zero."""
        from kicad_tools.analysis.net_status import NetStatus, NetStatusResult

        nets = []
        sda = NetStatus(net_number=1, net_name="SDA", total_pads=2)
        sda.connected_pads = [object()]
        sda.unconnected_pads = [object()]
        nets.append(sda)

        scl = NetStatus(net_number=2, net_name="SCL", total_pads=2)
        scl.connected_pads = [object()]
        scl.unconnected_pads = [object()]
        nets.append(scl)

        mock_result = NetStatusResult(nets=nets, total_nets=2)
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["signal_incomplete_named"] == ["SCL", "SDA"]
        assert status["signal_incomplete_auto_count"] == 0

    def test_no_zone_or_single_pad_nets(self, tmp_path):
        """When all nets are signal nets, zone/single-pad counts are zero."""
        mock_result = _make_net_status_result(
            complete_names=["SDA", "SCL"],
            incomplete_names=["MISO"],
            unrouted_names=[],
        )
        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        with patch(
            "kicad_tools.analysis.net_status.NetStatusAnalyzer.analyze",
            return_value=mock_result,
        ):
            status = collector.collect_net_status(None)

        assert status["zone_connected_count"] == 0
        assert status["zone_connected_nets"] == []
        assert status["single_pad_count"] == 0
        assert status["single_pad_nets"] == []
        assert status["signal_net_count"] == 3
        assert status["signal_complete_count"] == 2


# ---------------------------------------------------------------------------
# Narrative collector tests
# ---------------------------------------------------------------------------


class TestCollectNarrative:
    """Tests for collect_narrative and its sub-extractors."""

    def test_returns_all_expected_keys(self, tmp_path):
        """collect_narrative returns all five sub-section keys."""
        pcb_path = PROJECT_FIXTURES / "test_project.kicad_pcb"
        sch_path = PROJECT_FIXTURES / "test_project.kicad_sch"
        if not pcb_path.exists() or not sch_path.exists():
            pytest.skip("test_project fixture not found")

        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(pcb_path)
        collector = ReportDataCollector(pcb_path)
        result = collector.collect_narrative(sch_path, pcb)

        expected_keys = {
            "design_narrative",
            "functional_blocks",
            "interfaces",
            "power_architecture",
            "assembly_notes",
        }
        assert expected_keys == set(result.keys())

    def test_narrative_from_title_block(self, tmp_path):
        """Design narrative is extracted from title-block comments."""
        from unittest.mock import MagicMock

        from kicad_tools.schema.schematic import TitleBlock

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        # Create a mock schematic with title block comments
        mock_sch = MagicMock()
        mock_sch.title_block = TitleBlock(
            title="Audio Interface Board",
            comments={1: "I2S audio codec with USB input", 2: "Rev A prototype"},
        )
        mock_sch.sheets = []

        narrative = collector._extract_design_narrative(mock_sch, tmp_path / "test.kicad_sch")

        assert narrative is not None
        assert "Audio Interface Board" in narrative
        assert "I2S audio codec with USB input" in narrative
        assert "Rev A prototype" in narrative

    def test_narrative_returns_none_when_empty(self, tmp_path):
        """Design narrative is None when title block has no text."""
        from unittest.mock import MagicMock

        from kicad_tools.schema.schematic import TitleBlock

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = MagicMock()
        mock_sch.title_block = TitleBlock()
        mock_sch.sheets = []

        narrative = collector._extract_design_narrative(mock_sch, tmp_path / "test.kicad_sch")

        assert narrative is None

    def test_functional_blocks_from_sheets(self, tmp_path):
        """Functional blocks are extracted from hierarchical sheets."""
        from unittest.mock import MagicMock

        from kicad_tools.schema.schematic import SheetInstance

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = MagicMock()
        mock_sch.sheets = [
            SheetInstance(name="Power Supply", filename="power.kicad_sch", uuid="1"),
            SheetInstance(name="Audio DAC", filename="audio_dac.kicad_sch", uuid="2"),
        ]

        blocks = collector._extract_functional_blocks(mock_sch)

        assert blocks is not None
        assert len(blocks) == 2
        assert blocks[0]["name"] == "Power Supply"
        assert blocks[0]["filename"] == "power.kicad_sch"
        assert blocks[1]["name"] == "Audio DAC"

    def test_functional_blocks_none_when_no_sheets(self, tmp_path):
        """Functional blocks returns None when no hierarchical sheets."""
        from unittest.mock import MagicMock

        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = MagicMock()
        mock_sch.sheets = []

        blocks = collector._extract_functional_blocks(mock_sch)

        assert blocks is None

    def test_interface_detection_i2c(self, tmp_path):
        """I2C interface detected when SDA and SCL labels present."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(["SDA", "SCL", "VBUS"])
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        assert interfaces is not None
        protocols = [i["protocol"] for i in interfaces]
        assert "I2C" in protocols
        # USB should NOT be detected (needs D+ or D- as well as VBUS)
        assert "USB" not in protocols

    def test_interface_detection_spi(self, tmp_path):
        """SPI interface detected with MOSI/MISO/SCK labels."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(["MOSI", "MISO", "SCK", "CS"])
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        assert interfaces is not None
        protocols = [i["protocol"] for i in interfaces]
        assert "SPI" in protocols

    def test_interface_detection_case_insensitive(self, tmp_path):
        """Interface detection works with mixed case labels."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(["I2C1_SDA", "I2C1_SCL"])
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        assert interfaces is not None
        protocols = [i["protocol"] for i in interfaces]
        assert "I2C" in protocols

    def test_interface_detection_none_when_no_matches(self, tmp_path):
        """Returns None when no interface patterns match."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(["LED1", "BUTTON"])
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        assert interfaces is None

    def test_interface_detection_deterministic(self, tmp_path):
        """Repeated detection over identical labels yields identical rows.

        Regression test for issue #3574: set iteration order made the
        first-match-per-pattern choice hash-order dependent, so identical
        inputs produced different report rows across runs.
        """
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        # Ambiguous names from the board-06 report plus genuine UART nets,
        # in several insertion orders to perturb set layout.
        names = [
            "PCIE_RX+",
            "PCIE_RX-",
            "PCIE_TX+",
            "PCIE_TX-",
            "USB3_TX2+",
            "USB3_TX2-",
            "UART_TX",
            "UART_RX",
            "DEBUG_TXD",
            "DEBUG_RXD",
        ]
        results = []
        for ordering in (names, list(reversed(names)), sorted(names)):
            mock_sch = _make_mock_schematic_with_labels(ordering)
            results.append(collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch"))
        # Also re-run on the same ordering.
        mock_sch = _make_mock_schematic_with_labels(names)
        results.append(collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch"))

        assert all(r == results[0] for r in results[1:])

    def test_interface_detection_uart_excludes_high_speed_diff_pairs(self, tmp_path):
        """PCIE/USB3 differential nets never satisfy UART TX/RX patterns."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(
            ["PCIE_RX+", "PCIE_RX-", "PCIE_TX+", "PCIE_TX-", "USB3_TX2+", "USB3_TX2-"]
        )
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        if interfaces is not None:
            protocols = [i["protocol"] for i in interfaces]
            assert "UART" not in protocols

    def test_interface_detection_uart_still_detected_alongside_high_speed(self, tmp_path):
        """Genuine UART nets are detected; UART row contains only them."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(
            ["PCIE_RX+", "PCIE_RX-", "USB3_TX2+", "USB3_TX2-", "UART_TX", "UART_RX"]
        )
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        assert interfaces is not None
        uart_rows = [i for i in interfaces if i["protocol"] == "UART"]
        assert len(uart_rows) == 1
        assert uart_rows[0]["signals"] == ["UART_RX", "UART_TX"]

    def test_interface_detection_excludes_p_n_suffix_diff_pairs(self, tmp_path):
        """_P/_N polarity-suffixed nets are excluded from single-ended rows."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_labels(
            ["ETH_TX_P", "ETH_TX_N", "ETH_RX_P", "ETH_RX_N"]
        )
        interfaces = collector._detect_interfaces(mock_sch, tmp_path / "test.kicad_sch")

        if interfaces is not None:
            protocols = [i["protocol"] for i in interfaces]
            assert "UART" not in protocols

    def test_power_architecture_finds_rails(self, tmp_path):
        """Power architecture detects power symbols."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_power_symbols(
            ["+3V3", "+5V", "GND"],
            regulators=[],
        )
        result = collector._extract_power_architecture(mock_sch, tmp_path / "test.kicad_sch")

        assert result is not None
        rails = [r["rail"] for r in result if r["type"] == "power_symbol"]
        assert "+3V3" in rails
        assert "+5V" in rails
        assert "GND" in rails

    def test_power_architecture_finds_regulators(self, tmp_path):
        """Power architecture detects voltage regulators."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_power_symbols(
            ["+3V3"],
            regulators=[("U3", "AMS1117-3.3", "Regulator_Linear:AMS1117-3.3")],
        )
        result = collector._extract_power_architecture(mock_sch, tmp_path / "test.kicad_sch")

        assert result is not None
        regs = [r for r in result if r["type"] == "regulator"]
        assert len(regs) == 1
        assert regs[0]["rail"] == "U3"
        assert regs[0]["value"] == "AMS1117-3.3"

    def test_power_architecture_none_when_empty(self, tmp_path):
        """Returns None when no power symbols or regulators found."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_sch = _make_mock_schematic_with_power_symbols([], regulators=[])
        result = collector._extract_power_architecture(mock_sch, tmp_path / "test.kicad_sch")

        assert result is None

    def test_assembly_notes_fine_pitch(self, tmp_path):
        """Assembly notes detect fine-pitch packages."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_pcb = _make_mock_pcb_with_footprints(
            [
                ("U1", "Package_QFP:LQFP-48"),
                ("U2", "Package_BGA:BGA-256"),
                ("R1", "Resistor_SMD:R_0402"),
            ]
        )
        result = collector._extract_assembly_notes(mock_pcb)

        assert result is not None
        assert result["fine_pitch_count"] == 2
        assert "U1" in result["fine_pitch_parts"]
        assert "U2" in result["fine_pitch_parts"]

    def test_assembly_notes_none_when_simple_board(self, tmp_path):
        """Assembly notes returns None for simple boards with no special components."""
        collector = ReportDataCollector(tmp_path / "dummy.kicad_pcb")

        mock_pcb = _make_mock_pcb_with_footprints(
            [
                ("R1", "Resistor_SMD:R_0402"),
                ("C1", "Capacitor_SMD:C_0402"),
            ]
        )
        result = collector._extract_assembly_notes(mock_pcb)

        assert result is None

    def test_narrative_sub_extractor_fault_tolerance(self, tmp_path):
        """Individual sub-extractors survive errors without crashing the whole narrative."""
        from unittest.mock import MagicMock

        pcb_path = tmp_path / "dummy.kicad_pcb"
        pcb_path.touch()
        collector = ReportDataCollector(pcb_path)

        # Create a mock schematic where title_block raises
        mock_sch = MagicMock()
        mock_sch.title_block = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
        type(mock_sch).title_block = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        mock_sch.sheets = []
        mock_sch.global_labels = []
        mock_sch.labels = []
        mock_sch.symbols = []

        mock_pcb = MagicMock()
        mock_pcb.footprints = []

        # Patch Schematic.load to return our controlled mock
        with patch(
            "kicad_tools.schema.schematic.Schematic.load",
            return_value=mock_sch,
        ):
            result = collector.collect_narrative(tmp_path / "test.kicad_sch", mock_pcb)

        # The design_narrative sub-extractor should have failed but others should work
        assert result["design_narrative"] is None
        # functional_blocks should be None (no sheets)
        assert result["functional_blocks"] is None
        # interfaces should be None (no matching labels)
        assert result["interfaces"] is None
        # power_architecture should be None (no power symbols)
        assert result["power_architecture"] is None
        # assembly_notes should be None (no special footprints)
        assert result["assembly_notes"] is None


# ---------------------------------------------------------------------------
# Narrative test helpers
# ---------------------------------------------------------------------------


def _make_mock_schematic_with_labels(label_names):
    """Build a mock Schematic with specified global labels."""
    from unittest.mock import MagicMock

    mock_sch = MagicMock()
    mock_labels = []
    for name in label_names:
        lbl = MagicMock()
        lbl.text = name
        mock_labels.append(lbl)
    mock_sch.global_labels = mock_labels
    mock_sch.labels = []
    mock_sch.sheets = []
    return mock_sch


def _make_mock_schematic_with_power_symbols(rail_names, regulators):
    """Build a mock Schematic with power symbols and optional regulators.

    Args:
        rail_names: List of power rail names (e.g., ["+3V3", "GND"]).
        regulators: List of (reference, value, lib_id) tuples.
    """
    from unittest.mock import MagicMock

    mock_sch = MagicMock()
    symbols = []
    for name in rail_names:
        sym = MagicMock()
        sym.lib_id = f"power:{name}"
        sym.reference = f"#{name}"
        sym.properties = {"Value": MagicMock(value=name)}
        symbols.append(sym)
    for ref, value, lib_id in regulators:
        sym = MagicMock()
        sym.lib_id = lib_id
        sym.reference = ref
        sym.properties = {"Value": MagicMock(value=value)}
        symbols.append(sym)
    mock_sch.symbols = symbols
    mock_sch.sheets = []
    return mock_sch


def _make_mock_pcb_with_footprints(footprint_specs):
    """Build a mock PCB with specified footprints.

    Args:
        footprint_specs: List of (reference, footprint_name) tuples.
    """
    from unittest.mock import MagicMock

    mock_pcb = MagicMock()
    fps = []
    for ref, name in footprint_specs:
        fp = MagicMock()
        fp.name = name
        fp.reference = ref
        fps.append(fp)
    mock_pcb.footprints = fps
    return mock_pcb
