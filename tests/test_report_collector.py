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
        """Cost data has all four template-expected keys with correct values."""
        audit_result = _make_mock_audit_result()
        collector = ReportDataCollector(Path("dummy.kicad_pcb"))
        cost = collector.collect_cost(audit_result)

        assert cost is not None
        assert set(cost.keys()) == {"per_unit", "batch_qty", "batch_total", "currency"}
        # mock: total_cost=5.0, quantity=5 => per_unit=1.0
        assert cost["per_unit"] == 1.0
        assert cost["batch_qty"] == 5
        assert cost["batch_total"] == 5.0
        assert cost["currency"] == "USD"

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
        """cost.json contains per_unit, batch_qty, batch_total, currency."""
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
