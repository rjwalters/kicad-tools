"""Tests for kicad_tools.sync.reconciler module."""

import json
import math
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.schema.bom import BOM, BOMItem
from kicad_tools.sync.reconciler import (
    Reconciler,
    SyncAnalysis,
    SyncChange,
    SyncMatch,
)


class TestSyncMatch:
    """Tests for SyncMatch dataclass."""

    def test_valid_match_creation(self):
        """Test creating a valid sync match."""
        match = SyncMatch(
            schematic_ref="R1",
            pcb_ref="R1",
            confidence="high",
            match_type="exact",
        )
        assert match.schematic_ref == "R1"
        assert match.pcb_ref == "R1"
        assert match.confidence == "high"
        assert match.match_type == "exact"
        assert match.actions == ()

    def test_match_with_actions(self):
        """Test creating a match with actions."""
        actions = (
            {"type": "update_value", "reference": "R1", "old_value": "10k", "new_value": "4.7k"},
        )
        match = SyncMatch(
            schematic_ref="R1",
            pcb_ref="R1",
            confidence="high",
            match_type="exact",
            actions=actions,
        )
        assert len(match.actions) == 1
        assert match.actions[0]["type"] == "update_value"

    def test_invalid_confidence_raises(self):
        """Test that invalid confidence raises ValueError."""
        with pytest.raises(ValueError, match="confidence must be"):
            SyncMatch(
                schematic_ref="R1",
                pcb_ref="R1",
                confidence="invalid",
                match_type="exact",
            )

    def test_invalid_match_type_raises(self):
        """Test that invalid match_type raises ValueError."""
        with pytest.raises(ValueError, match="match_type must be"):
            SyncMatch(
                schematic_ref="R1",
                pcb_ref="R1",
                confidence="high",
                match_type="invalid",
            )

    def test_to_dict(self):
        """Test JSON serialization."""
        match = SyncMatch(
            schematic_ref="R1",
            pcb_ref="R2",
            confidence="medium",
            match_type="value_footprint",
            actions=({"type": "rename", "old_value": "R2", "new_value": "R1"},),
        )
        d = match.to_dict()
        assert d["schematic_ref"] == "R1"
        assert d["pcb_ref"] == "R2"
        assert d["confidence"] == "medium"
        assert d["match_type"] == "value_footprint"
        assert len(d["actions"]) == 1


class TestSyncChange:
    """Tests for SyncChange dataclass."""

    def test_change_creation(self):
        """Test creating a sync change record."""
        change = SyncChange(
            reference="R1",
            change_type="update_value",
            old_value="10k",
            new_value="4.7k",
            applied=True,
        )
        assert change.reference == "R1"
        assert change.applied is True

    def test_change_default_not_applied(self):
        """Test that applied defaults to False."""
        change = SyncChange(
            reference="R1",
            change_type="rename",
            old_value="R2",
            new_value="R1",
        )
        assert change.applied is False

    def test_to_dict(self):
        """Test JSON serialization."""
        change = SyncChange(
            reference="R1",
            change_type="update_value",
            old_value="10k",
            new_value="4.7k",
        )
        d = change.to_dict()
        assert d["change_type"] == "update_value"
        assert d["old_value"] == "10k"
        assert d["new_value"] == "4.7k"


class TestSyncAnalysis:
    """Tests for SyncAnalysis dataclass."""

    def test_empty_analysis_is_in_sync(self):
        """Test that empty analysis reports in-sync."""
        analysis = SyncAnalysis()
        assert analysis.is_in_sync
        assert not analysis.has_actionable_items

    def test_analysis_with_orphans_not_in_sync(self):
        """Test that orphans make analysis out-of-sync."""
        analysis = SyncAnalysis(schematic_orphans=["R5"])
        assert not analysis.is_in_sync

    def test_analysis_with_value_mismatches(self):
        """Test analysis with value mismatches."""
        analysis = SyncAnalysis(
            value_mismatches=[{"reference": "R1", "schematic_value": "10k", "pcb_value": "4.7k"}]
        )
        assert not analysis.is_in_sync
        assert analysis.has_actionable_items

    def test_confidence_filtering(self):
        """Test filtering matches by confidence level."""
        analysis = SyncAnalysis(
            matches=[
                SyncMatch("R1", "R1", "high", "exact"),
                SyncMatch("R2", "R3", "medium", "value_footprint"),
                SyncMatch("C1", "C5", "low", "footprint_only"),
            ]
        )
        assert len(analysis.high_confidence_matches) == 1
        assert len(analysis.medium_confidence_matches) == 1
        assert len(analysis.low_confidence_matches) == 1

    def test_summary_in_sync(self):
        """Test summary when in sync."""
        analysis = SyncAnalysis()
        assert "in sync" in analysis.summary().lower()

    def test_summary_with_mismatches(self):
        """Test summary with mismatches."""
        analysis = SyncAnalysis(
            matches=[SyncMatch("R1", "R1", "high", "exact")],
            schematic_orphans=["R5"],
        )
        summary = analysis.summary()
        assert "Matched components:" in summary
        assert "Schematic-only:" in summary

    def test_to_dict(self):
        """Test JSON serialization round-trip."""
        analysis = SyncAnalysis(
            matches=[SyncMatch("R1", "R1", "high", "exact")],
            schematic_orphans=["R5"],
            pcb_orphans=["C3"],
            value_mismatches=[{"reference": "U1", "schematic_value": "ATmega", "pcb_value": "LM"}],
        )
        d = analysis.to_dict()
        assert d["is_in_sync"] is False
        assert len(d["matches"]) == 1
        assert d["schematic_orphans"] == ["R5"]
        assert d["pcb_orphans"] == ["C3"]
        assert d["summary"]["total_matches"] == 1

    def test_add_footprint_actions_in_has_actionable(self):
        """Test that add_footprint_actions makes analysis actionable."""
        analysis = SyncAnalysis(
            add_footprint_actions=[
                {"type": "add_footprint", "reference": "R5", "footprint": "R_0402", "value": "10k"}
            ]
        )
        assert analysis.has_actionable_items

    def test_add_footprint_actions_in_to_dict(self):
        """Test that add_footprint_actions appear in to_dict output."""
        actions = [
            {"type": "add_footprint", "reference": "R5", "footprint": "R_0402", "value": "10k"}
        ]
        analysis = SyncAnalysis(add_footprint_actions=actions)
        d = analysis.to_dict()
        assert d["add_footprint_actions"] == actions
        assert d["summary"]["add_footprint"] == 1

    def test_summary_with_add_footprint(self):
        """Test summary includes add_footprint count."""
        analysis = SyncAnalysis(
            matches=[SyncMatch("R1", "R1", "high", "exact")],
            schematic_orphans=["R5"],
            add_footprint_actions=[
                {"type": "add_footprint", "reference": "R5", "footprint": "R_0402", "value": "10k"}
            ],
        )
        summary = analysis.summary()
        assert "Add footprint:" in summary


class TestReconciler:
    """Tests for Reconciler class."""

    def test_init_requires_paths(self):
        """Test that Reconciler requires project or both schematic+pcb."""
        with pytest.raises(ValueError, match="Must provide"):
            Reconciler()

    def test_init_missing_project_file(self):
        """Test that missing project file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Project file not found"):
            Reconciler(project="/nonexistent/project.kicad_pro")

    def test_init_missing_schematic(self):
        """Test that missing schematic file raises FileNotFoundError."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as f:
            pcb_path = f.name
        try:
            with pytest.raises(FileNotFoundError, match="Schematic not found"):
                Reconciler(schematic="/nonexistent.kicad_sch", pcb=pcb_path)
        finally:
            Path(pcb_path).unlink()

    def test_init_missing_pcb(self):
        """Test that missing PCB file raises FileNotFoundError."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as f:
            sch_path = f.name
        try:
            with pytest.raises(FileNotFoundError, match="PCB not found"):
                Reconciler(schematic=sch_path, pcb="/nonexistent.kicad_pcb")
        finally:
            Path(sch_path).unlink()


class TestReconcilerAnalyze:
    """Tests for Reconciler.analyze() using mocked schematic/PCB data."""

    def _make_bom_item(self, ref: str, value: str = "", footprint: str = "", lib_id: str = ""):
        """Create a BOMItem for mocking extract_bom."""
        return BOMItem(
            reference=ref,
            value=value,
            footprint=footprint,
            lib_id=lib_id or "",
            in_bom=True,
        )

    def _make_mock_footprint(self, ref: str, value: str = "", name: str = ""):
        """Create a mock PCB footprint."""
        fp = MagicMock()
        fp.reference = ref
        fp.value = value
        fp.name = name
        fp.pads = []
        return fp

    def _make_reconciler_with_mocks(self, bom_items, footprints):
        """Create a Reconciler with mocked file loading and extract_bom."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        mock_bom = BOM(items=bom_items, source=sch_path)
        mock_pcb = MagicMock()
        mock_pcb.footprints = footprints

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)

        return reconciler, mock_bom, mock_pcb, sch_path, pcb_path

    def _run_analyze(self, reconciler, mock_bom, mock_pcb):
        """Run analyze() with mocked dependencies."""
        mock_checker = MagicMock()
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with (
            patch(
                "kicad_tools.validate.consistency.SchematicPCBChecker",
                return_value=mock_checker,
            ),
            patch(
                "kicad_tools.sync.reconciler.extract_bom",
                return_value=mock_bom,
            ),
        ):
            return reconciler.analyze()

    def test_analyze_in_sync(self):
        """Test analysis when schematic and PCB are in sync."""
        bom_items = [self._make_bom_item("R1", "10k", "Resistor_SMD:R_0402")]
        footprints = [self._make_mock_footprint("R1", "10k", "Resistor_SMD:R_0402")]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert analysis.is_in_sync
        assert len(analysis.matches) == 1
        assert analysis.matches[0].confidence == "high"
        assert len(analysis.schematic_orphans) == 0
        assert len(analysis.pcb_orphans) == 0

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_value_mismatch(self):
        """Test analysis detects value mismatches."""
        bom_items = [self._make_bom_item("R1", "10k", "Resistor_SMD:R_0402")]
        footprints = [self._make_mock_footprint("R1", "4.7k", "Resistor_SMD:R_0402")]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert not analysis.is_in_sync
        assert len(analysis.value_mismatches) == 1
        assert analysis.value_mismatches[0]["schematic_value"] == "10k"
        assert analysis.value_mismatches[0]["pcb_value"] == "4.7k"

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_orphans(self):
        """Test analysis detects orphans in both directions."""
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402"),
            self._make_bom_item("R2", "22k", "R_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
            self._make_mock_footprint("C1", "100nF", "C_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert "R2" in analysis.schematic_orphans
        assert "C1" in analysis.pcb_orphans

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_medium_confidence_match(self):
        """Test that value+footprint matching produces medium confidence."""
        bom_items = [
            self._make_bom_item("R1", "10k", "Resistor_SMD:R_0402"),
            self._make_bom_item("R5", "22k", "Resistor_SMD:R_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "Resistor_SMD:R_0402"),
            self._make_mock_footprint("R99", "22k", "Resistor_SMD:R_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        medium = analysis.medium_confidence_matches
        assert len(medium) == 1
        assert medium[0].schematic_ref == "R5"
        assert medium[0].pcb_ref == "R99"
        assert medium[0].match_type == "value_footprint"

        # R5 and R99 should not appear in orphans anymore
        assert "R5" not in analysis.schematic_orphans
        assert "R99" not in analysis.pcb_orphans

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_no_mismatches_is_in_sync(self):
        """Test that matching refs with same properties reports in-sync."""
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402"),
            self._make_bom_item("C1", "100nF", "C_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
            self._make_mock_footprint("C1", "100nF", "C_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert analysis.is_in_sync

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_hierarchical_components(self):
        """Test that hierarchical sub-sheet components are included via extract_bom."""
        # Simulate components from root sheet and a sub-sheet
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402", lib_id="Device:R"),
            self._make_bom_item("U1", "ATmega328P", "TQFP-32", lib_id="MCU:ATmega328P"),
            # Sub-sheet component
            self._make_bom_item("R10", "4.7k", "R_0402", lib_id="Device:R"),
            self._make_bom_item("C5", "100nF", "C_0402", lib_id="Device:C"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
            self._make_mock_footprint("U1", "ATmega328P", "TQFP-32"),
            self._make_mock_footprint("R10", "4.7k", "R_0402"),
            self._make_mock_footprint("C5", "100nF", "C_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert analysis.is_in_sync
        assert len(analysis.matches) == 4
        assert len(analysis.schematic_orphans) == 0
        assert len(analysis.pcb_orphans) == 0

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_hierarchical_missing_from_pcb(self):
        """Test that sub-sheet components missing from PCB are reported as orphans."""
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402", lib_id="Device:R"),
            # Sub-sheet components not in PCB
            self._make_bom_item("R10", "4.7k", "R_0402", lib_id="Device:R"),
            self._make_bom_item("C5", "100nF", "C_0402", lib_id="Device:C"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert not analysis.is_in_sync
        assert "R10" in analysis.schematic_orphans
        assert "C5" in analysis.schematic_orphans

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_skips_virtual_and_power(self):
        """Test that virtual/power symbols and DNP components are skipped."""
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402", lib_id="Device:R"),
            BOMItem(
                reference="PWR1",
                value="+3V3",
                footprint="",
                lib_id="power:+3V3",
                in_bom=False,
            ),
            BOMItem(
                reference="R2",
                value="100",
                footprint="R_0402",
                lib_id="Device:R",
                dnp=True,
            ),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert analysis.is_in_sync
        assert len(analysis.matches) == 1
        # PWR1 and R2 (DNP) should not appear anywhere
        assert "PWR1" not in analysis.schematic_orphans
        assert "R2" not in analysis.schematic_orphans

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_add_footprint_actions(self):
        """Test that add_footprint actions are generated for schematic orphans."""
        bom_items = [
            self._make_bom_item("R1", "10k", "R_0402", lib_id="Device:R"),
            self._make_bom_item("R2", "22k", "Resistor_SMD:R_0402", lib_id="Device:R"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
        ]

        reconciler, mock_bom, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            bom_items, footprints
        )

        analysis = self._run_analyze(reconciler, mock_bom, mock_pcb)

        assert len(analysis.add_footprint_actions) == 1
        action = analysis.add_footprint_actions[0]
        assert action["type"] == "add_footprint"
        assert action["reference"] == "R2"
        assert action["footprint"] == "Resistor_SMD:R_0402"
        assert action["value"] == "22k"
        assert action["lib_id"] == "Device:R"

        Path(sch_path).unlink()
        Path(pcb_path).unlink()


class TestReconcilerApplyAddFootprint:
    """Tests for Reconciler._apply_add_footprint() method."""

    def test_add_footprint_dry_run(self):
        """Test that add_footprint in dry-run mode produces unapplied SyncChange."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "add_footprint",
            "reference": "R5",
            "footprint": "Resistor_SMD:R_0402",
            "value": "10k",
            "lib_id": "Device:R",
        }

        mock_pcb = MagicMock()
        change = reconciler._apply_add_footprint(mock_pcb, action, dry_run=True, x=10.0, y=100.0)

        assert change is not None
        assert change.change_type == "add_footprint"
        assert change.reference == "R5"
        assert change.old_value == ""
        assert "Resistor_SMD:R_0402" in change.new_value
        assert "10k" in change.new_value
        assert change.applied is False
        # PCB.add_footprint() should NOT be called in dry-run
        mock_pcb.add_footprint.assert_not_called()

    def test_add_footprint_applied(self):
        """Test that add_footprint calls pcb.add_footprint when not dry-run."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "add_footprint",
            "reference": "C1",
            "footprint": "Capacitor_SMD:C_0402",
            "value": "100nF",
        }

        mock_pcb = MagicMock()
        change = reconciler._apply_add_footprint(mock_pcb, action, dry_run=False, x=20.0, y=110.0)

        assert change is not None
        assert change.applied is True
        assert change.change_type == "add_footprint"
        assert change.reference == "C1"
        mock_pcb.add_footprint.assert_called_once_with(
            library_id="Capacitor_SMD:C_0402",
            reference="C1",
            x=20.0,
            y=110.0,
            rotation=0.0,
            layer="F.Cu",
            value="100nF",
        )

    def test_add_footprint_library_not_found(self):
        """Test graceful fallback when library is not found."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "add_footprint",
            "reference": "U1",
            "footprint": "CustomLib:CustomPart",
            "value": "MyChip",
        }

        mock_pcb = MagicMock()
        mock_pcb.add_footprint.side_effect = FileNotFoundError("Footprint not found")
        change = reconciler._apply_add_footprint(mock_pcb, action, dry_run=False, x=10.0, y=10.0)

        assert change is not None
        assert change.applied is False
        assert "error:" in change.new_value
        assert change.reference == "U1"

    def test_add_footprint_value_error(self):
        """Test graceful fallback when KiCad libraries are not installed."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "add_footprint",
            "reference": "R1",
            "footprint": "R_0402",
            "value": "10k",
        }

        mock_pcb = MagicMock()
        mock_pcb.add_footprint.side_effect = ValueError("KiCad library path not found")
        change = reconciler._apply_add_footprint(mock_pcb, action, dry_run=False, x=10.0, y=10.0)

        assert change is not None
        assert change.applied is False
        assert "error:" in change.new_value


class TestReconcilerApplyIntegration:
    """Integration tests for Reconciler.apply() with add_footprint actions."""

    def _make_reconciler(self):
        """Create a Reconciler with temp file paths."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)
        return reconciler, sch_path, pcb_path

    def test_apply_add_footprint_to_pcb(self):
        """Test that apply() adds footprints to the PCB via PCB.add_footprint()."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "R5",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                    "lib_id": "Device:R",
                },
                {
                    "type": "add_footprint",
                    "reference": "C3",
                    "footprint": "Capacitor_SMD:C_0402",
                    "value": "100nF",
                    "lib_id": "Device:C",
                },
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb._sexp = MagicMock()

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 2
        assert all(c.change_type == "add_footprint" for c in changes)
        assert all(c.applied for c in changes)
        assert changes[0].reference == "R5"
        assert changes[1].reference == "C3"

        # Verify add_footprint was called twice with grid positions
        assert mock_pcb.add_footprint.call_count == 2
        # Verify save was called
        mock_pcb.save.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_dry_run_no_file_changes(self):
        """Test that dry-run mode does not modify the PCB file."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "R5",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                    "lib_id": "Device:R",
                },
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        # PCB.add_footprint() should NOT be called
        mock_pcb.add_footprint.assert_not_called()
        # PCB.save() should NOT be called
        mock_pcb.save.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_preserves_existing_actions(self):
        """Test that rename and update_value actions still work after refactor."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        mock_fp = MagicMock()
        mock_fp_text_ref = MagicMock()
        mock_fp_text_ref.get_string.return_value = "reference"
        mock_fp_text_val = MagicMock()
        mock_fp_text_val.get_string.return_value = "value"
        mock_fp.find_children.return_value = [mock_fp_text_ref, mock_fp_text_val]

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="R1",
                    pcb_ref="R99",
                    confidence="high",
                    match_type="value_footprint",
                    actions=(
                        {
                            "type": "rename",
                            "reference": "R99",
                            "old_value": "R99",
                            "new_value": "R1",
                        },
                    ),
                ),
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_value",
                            "reference": "C1",
                            "old_value": "100nF",
                            "new_value": "10nF",
                        },
                    ),
                ),
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb._sexp = MagicMock()

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.cli.pcb_modify.find_footprint_sexp",
                return_value=mock_fp,
            ),
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 2
        assert changes[0].change_type == "rename"
        assert changes[0].applied is True
        assert changes[1].change_type == "update_value"
        assert changes[1].applied is True
        mock_pcb.save.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_mixed_add_and_rename(self):
        """Test applying both rename and add_footprint actions together."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        mock_fp = MagicMock()
        mock_fp_text_ref = MagicMock()
        mock_fp_text_ref.get_string.return_value = "reference"
        mock_fp.find_children.return_value = [mock_fp_text_ref]

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="R1",
                    pcb_ref="R99",
                    confidence="high",
                    match_type="value_footprint",
                    actions=(
                        {
                            "type": "rename",
                            "reference": "R99",
                            "old_value": "R99",
                            "new_value": "R1",
                        },
                    ),
                ),
            ],
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "C5",
                    "footprint": "Capacitor_SMD:C_0402",
                    "value": "100nF",
                    "lib_id": "Device:C",
                },
            ],
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb._sexp = MagicMock()

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.cli.pcb_modify.find_footprint_sexp",
                return_value=mock_fp,
            ),
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 2
        rename_changes = [c for c in changes if c.change_type == "rename"]
        add_changes = [c for c in changes if c.change_type == "add_footprint"]
        assert len(rename_changes) == 1
        assert len(add_changes) == 1
        assert rename_changes[0].applied is True
        assert add_changes[0].applied is True

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_net_assignment_called_after_add(self):
        """Test that net assignment is performed after adding footprints."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "R1",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                    "lib_id": "Device:R",
                },
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        mock_netlist = MagicMock()
        mock_netlist.nets = []

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.operations.netlist.export_netlist",
                return_value=mock_netlist,
            ) as mock_export,
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 1
        assert changes[0].applied is True
        # Verify export_netlist was called (for smart placement and net assignment)
        mock_export.assert_called_with(sch_path)
        assert mock_export.call_count >= 1
        mock_pcb.assign_nets_from_netlist.assert_called_once_with(mock_netlist)

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_library_not_found_continues(self):
        """Test that when one footprint fails, others still proceed."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "U1",
                    "footprint": "CustomLib:NoSuchPart",
                    "value": "MyChip",
                },
                {
                    "type": "add_footprint",
                    "reference": "R1",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                },
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        # First call fails, second succeeds
        mock_pcb.add_footprint.side_effect = [
            FileNotFoundError("Not found"),
            MagicMock(),
        ]

        mock_netlist = MagicMock()
        mock_netlist.nets = []

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.operations.netlist.export_netlist",
                return_value=mock_netlist,
            ),
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 2
        assert changes[0].applied is False  # U1 failed
        assert changes[0].reference == "U1"
        assert changes[1].applied is True  # R1 succeeded
        assert changes[1].reference == "R1"

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_backup_created(self):
        """Test that a .bak file is created before modifying."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "R1",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                },
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch("kicad_tools.sync.reconciler.shutil.copy2") as mock_copy,
        ):
            reconciler.apply(analysis, dry_run=False)

        # Verify backup was created
        mock_copy.assert_called_once()
        backup_call = mock_copy.call_args
        assert str(backup_call[0][1]).endswith(".kicad_pcb.bak")

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_compute_placement_with_board_outline(self):
        """Test placement position is computed below the board outline.

        get_board_outline() now returns board-relative coordinates, so
        the mock returns (0,0)-(100,80) instead of sheet-absolute coords.
        """
        reconciler = Reconciler.__new__(Reconciler)

        mock_pcb = MagicMock()
        # Board outline: a 100x80mm rectangle, already board-relative
        mock_pcb.get_board_outline.return_value = [
            (0.0, 0.0),
            (100.0, 0.0),
            (100.0, 80.0),
            (0.0, 80.0),
        ]
        mock_pcb.board_origin = (50.0, 50.0)

        x, y, col = reconciler._compute_placement_start(mock_pcb)

        # min_x=0 -> start_x = 0
        assert x == 0.0
        # max_y=80 -> start_y = 80 + 10 = 90
        assert y == 90.0
        assert col == 0

    def test_compute_placement_no_outline(self):
        """Test default placement when no board outline exists."""
        reconciler = Reconciler.__new__(Reconciler)

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        x, y, col = reconciler._compute_placement_start(mock_pcb)

        assert x == 10.0
        assert y == 10.0
        assert col == 0


class TestReconcilerApplyUpdateFootprint:
    """Tests for Reconciler._apply_update_footprint() method."""

    def test_update_footprint_dry_run(self):
        """Test that update_footprint in dry-run mode produces unapplied SyncChange."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "C1",
            "old_value": "Capacitor_SMD:C_0402_1005Metric",
            "new_value": "Capacitor_SMD:C_0805_2012Metric",
        }

        mock_pcb = MagicMock()
        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=True)

        assert change is not None
        assert change.change_type == "update_footprint"
        assert change.reference == "C1"
        assert change.old_value == "Capacitor_SMD:C_0402_1005Metric"
        assert change.new_value == "Capacitor_SMD:C_0805_2012Metric"
        assert change.applied is False
        # PCB should NOT be modified in dry-run
        mock_pcb.get_footprint.assert_not_called()
        mock_pcb.remove_footprint.assert_not_called()
        mock_pcb.add_footprint.assert_not_called()

    def test_update_footprint_applied(self):
        """Test that update_footprint swaps the footprint preserving position."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "C1",
            "old_value": "Capacitor_SMD:C_0402_1005Metric",
            "new_value": "Capacitor_SMD:C_0805_2012Metric",
        }

        # Mock old footprint
        mock_old_fp = MagicMock()
        mock_old_fp.position = (50.0, 30.0)
        mock_old_fp.rotation = 90.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "100nF"
        mock_pad1 = MagicMock()
        mock_pad1.number = "1"
        mock_pad1.net_number = 1
        mock_pad1.net_name = "GND"
        mock_pad2 = MagicMock()
        mock_pad2.number = "2"
        mock_pad2.net_number = 2
        mock_pad2.net_name = "+3V3"
        mock_old_fp.pads = [mock_pad1, mock_pad2]

        # Mock new footprint (returned by add_footprint)
        mock_new_fp = MagicMock()
        mock_new_pad1 = MagicMock()
        mock_new_pad1.number = "1"
        mock_new_pad2 = MagicMock()
        mock_new_pad2.number = "2"
        mock_new_fp.pads = [mock_new_pad1, mock_new_pad2]

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.get_pad_position.return_value = (50.0, 30.0)
        mock_pcb.segments_in_net.return_value = []
        mock_pcb.add_footprint.return_value = mock_new_fp

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is True
        assert change.change_type == "update_footprint"
        assert change.reference == "C1"

        # Verify old footprint was removed
        mock_pcb.remove_footprint.assert_called_once_with("C1")

        # Verify new footprint was added with a temporary reference to avoid
        # remove_footprint accidentally deleting both old and new
        mock_pcb.add_footprint.assert_called_once_with(
            library_id="Capacitor_SMD:C_0805_2012Metric",
            reference="__SWAP_TEMP__C1",
            x=50.0,
            y=30.0,
            rotation=90.0,
            layer="F.Cu",
            value="100nF",
        )

        # Verify the temporary reference was renamed to the real one
        mock_pcb.update_footprint_reference.assert_called_once_with("__SWAP_TEMP__C1", "C1")

        # Verify net assignment was called for each pad
        mock_pcb.assign_net_to_footprint_pad.assert_any_call("C1", "1", "GND")
        mock_pcb.assign_net_to_footprint_pad.assert_any_call("C1", "2", "+3V3")

    def test_update_footprint_preserves_back_layer(self):
        """Test that B.Cu layer is preserved during swap."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "R1",
            "old_value": "Resistor_SMD:R_0402",
            "new_value": "Resistor_SMD:R_0805",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (10.0, 20.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "B.Cu"
        mock_old_fp.value = "10k"
        mock_old_fp.pads = []

        mock_new_fp = MagicMock()
        mock_new_fp.pads = []

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.return_value = mock_new_fp

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is True
        # Verify layer was preserved
        call_kwargs = mock_pcb.add_footprint.call_args
        assert call_kwargs[1]["layer"] == "B.Cu"

    def test_update_footprint_removes_connected_traces(self):
        """Test that traces connected to old pad positions are removed."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "C1",
            "old_value": "Capacitor_SMD:C_0402",
            "new_value": "Capacitor_SMD:C_0805",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (50.0, 30.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "100nF"
        mock_pad1 = MagicMock()
        mock_pad1.number = "1"
        mock_pad1.net_number = 1
        mock_pad1.net_name = "GND"
        mock_old_fp.pads = [mock_pad1]

        # Create a segment that touches the pad position
        mock_seg = MagicMock()
        mock_seg.start = (49.49, 30.0)  # Close to pad position
        mock_seg.end = (45.0, 30.0)
        mock_seg.uuid = "seg-uuid-1"

        mock_new_fp = MagicMock()
        mock_new_fp.pads = [MagicMock(number="1")]

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.get_pad_position.return_value = (49.49, 30.0)
        mock_pcb.segments_in_net.return_value = [mock_seg]
        mock_pcb.add_footprint.return_value = mock_new_fp

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is True
        # Verify connected segments were removed
        mock_pcb.remove_segments.assert_called_once()
        removed_segs = mock_pcb.remove_segments.call_args[0][0]
        assert len(removed_segs) == 1
        assert removed_segs[0].uuid == "seg-uuid-1"

    def test_update_footprint_reports_affected_nets(self):
        """Test that affected nets are reported in the new_value field."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "C1",
            "old_value": "C_0402",
            "new_value": "C_0805",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (50.0, 30.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "100nF"
        mock_pad1 = MagicMock()
        mock_pad1.number = "1"
        mock_pad1.net_number = 1
        mock_pad1.net_name = "GND"
        mock_pad2 = MagicMock()
        mock_pad2.number = "2"
        mock_pad2.net_number = 2
        mock_pad2.net_name = "+3V3"
        mock_old_fp.pads = [mock_pad1, mock_pad2]

        # Create segments touching each pad
        mock_seg1 = MagicMock()
        mock_seg1.start = (49.49, 30.0)
        mock_seg1.end = (45.0, 30.0)
        mock_seg1.uuid = "seg-1"
        mock_seg2 = MagicMock()
        mock_seg2.start = (50.51, 30.0)
        mock_seg2.end = (55.0, 30.0)
        mock_seg2.uuid = "seg-2"

        mock_new_fp = MagicMock()
        mock_new_fp.pads = [MagicMock(number="1"), MagicMock(number="2")]

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.get_pad_position.side_effect = lambda ref, pad: {
            "1": (49.49, 30.0),
            "2": (50.51, 30.0),
        }.get(pad)
        mock_pcb.segments_in_net.side_effect = lambda net: {
            1: [mock_seg1],
            2: [mock_seg2],
        }.get(net, [])
        mock_pcb.add_footprint.return_value = mock_new_fp

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is True
        assert "re-route" in change.new_value
        assert "GND" in change.new_value
        assert "+3V3" in change.new_value

    def test_update_footprint_missing_reference(self):
        """Test that missing footprint reference returns None."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "MISSING",
            "old_value": "C_0402",
            "new_value": "C_0805",
        }

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = None

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is None

    def test_update_footprint_library_not_found(self):
        """Test graceful fallback when new footprint library is not found."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "U1",
            "old_value": "Package_SO:SOIC-8",
            "new_value": "CustomLib:NoSuchPart",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (10.0, 10.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "IC1"
        mock_old_fp.pads = []

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.side_effect = FileNotFoundError("Not found")

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is False
        assert "error:" in change.new_value

    def test_update_footprint_no_removal_on_add_failure(self):
        """Test that old footprint is NOT removed when add_footprint fails.

        Regression test: previously, remove_footprint was called before
        add_footprint, so a failure in add_footprint would leave the board
        with the old footprint deleted — data loss.
        """
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "U1",
            "old_value": "Package_SO:SOIC-8",
            "new_value": "CustomLib:BadFootprint",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (10.0, 20.0)
        mock_old_fp.rotation = 90.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "IC1"
        mock_old_fp.pads = []

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.side_effect = FileNotFoundError("library not found")

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is False
        assert "error:" in change.new_value
        # The critical assertion: remove_footprint must NOT have been called
        mock_pcb.remove_footprint.assert_not_called()

    def test_update_footprint_no_removal_on_value_error(self):
        """Test that old footprint is NOT removed when add_footprint raises ValueError."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_footprint",
            "reference": "R5",
            "old_value": "Resistor_SMD:R_0402",
            "new_value": "Resistor_SMD:R_invalid",
        }

        mock_old_fp = MagicMock()
        mock_old_fp.position = (5.0, 5.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "10k"
        mock_old_fp.pads = []

        mock_pcb = MagicMock()
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.side_effect = ValueError("invalid footprint")

        change = reconciler._apply_update_footprint(mock_pcb, action, dry_run=False)

        assert change is not None
        assert change.applied is False
        assert "error:" in change.new_value
        mock_pcb.remove_footprint.assert_not_called()


class TestReconcilerApplyUpdateFootprintIntegration:
    """Integration tests for update_footprint via Reconciler.apply()."""

    def _make_reconciler(self):
        """Create a Reconciler with temp file paths."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)
        return reconciler, sch_path, pcb_path

    def test_apply_update_footprint_to_pcb(self):
        """Test that apply() swaps footprints via update_footprint actions."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_footprint",
                            "reference": "C1",
                            "old_value": "Capacitor_SMD:C_0402",
                            "new_value": "Capacitor_SMD:C_0805",
                        },
                    ),
                ),
            ],
            footprint_mismatches=[
                {
                    "reference": "C1",
                    "schematic_footprint": "Capacitor_SMD:C_0805",
                    "pcb_footprint": "Capacitor_SMD:C_0402",
                },
            ],
        )

        # Mock old footprint
        mock_old_fp = MagicMock()
        mock_old_fp.position = (50.0, 30.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "100nF"
        mock_old_fp.pads = []

        mock_new_fp = MagicMock()
        mock_new_fp.pads = []

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.return_value = mock_new_fp
        mock_pcb._sexp = MagicMock()

        mock_netlist = MagicMock()
        mock_netlist.nets = []

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.operations.netlist.export_netlist",
                return_value=mock_netlist,
            ),
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 1
        assert changes[0].change_type == "update_footprint"
        assert changes[0].applied is True
        assert changes[0].reference == "C1"

        # Verify PCB was saved
        mock_pcb.save.assert_called_once()
        # Verify net assignment was run
        mock_pcb.assign_nets_from_netlist.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_update_footprint_dry_run(self):
        """Test that dry-run does not modify the PCB for update_footprint."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_footprint",
                            "reference": "C1",
                            "old_value": "C_0402",
                            "new_value": "C_0805",
                        },
                    ),
                ),
            ],
            footprint_mismatches=[
                {
                    "reference": "C1",
                    "schematic_footprint": "C_0805",
                    "pcb_footprint": "C_0402",
                },
            ],
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        assert changes[0].change_type == "update_footprint"
        # PCB should NOT be modified
        mock_pcb.get_footprint.assert_not_called()
        mock_pcb.remove_footprint.assert_not_called()
        mock_pcb.add_footprint.assert_not_called()
        mock_pcb.save.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_mixed_update_and_add_footprint(self):
        """Test applying both update_footprint and add_footprint actions."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        mock_old_fp = MagicMock()
        mock_old_fp.position = (50.0, 30.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "100nF"
        mock_old_fp.pads = []

        mock_new_fp = MagicMock()
        mock_new_fp.pads = []

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_footprint",
                            "reference": "C1",
                            "old_value": "C_0402",
                            "new_value": "C_0805",
                        },
                    ),
                ),
            ],
            add_footprint_actions=[
                {
                    "type": "add_footprint",
                    "reference": "R5",
                    "footprint": "Resistor_SMD:R_0402",
                    "value": "10k",
                },
            ],
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.return_value = mock_new_fp
        mock_pcb._sexp = MagicMock()

        mock_netlist = MagicMock()
        mock_netlist.nets = []

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch(
                "kicad_tools.operations.netlist.export_netlist",
                return_value=mock_netlist,
            ),
        ):
            changes = reconciler.apply(analysis, dry_run=False)

        update_changes = [c for c in changes if c.change_type == "update_footprint"]
        add_changes = [c for c in changes if c.change_type == "add_footprint"]
        assert len(update_changes) == 1
        assert len(add_changes) == 1
        assert update_changes[0].applied is True
        assert add_changes[0].applied is True

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_apply_update_footprint_creates_backup(self):
        """Test that a backup file is created before modifying for update_footprint."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        mock_old_fp = MagicMock()
        mock_old_fp.position = (10.0, 10.0)
        mock_old_fp.rotation = 0.0
        mock_old_fp.layer = "F.Cu"
        mock_old_fp.value = "1k"
        mock_old_fp.pads = []

        mock_new_fp = MagicMock()
        mock_new_fp.pads = []

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="R1",
                    pcb_ref="R1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_footprint",
                            "reference": "R1",
                            "old_value": "R_0402",
                            "new_value": "R_0805",
                        },
                    ),
                ),
            ],
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.get_footprint.return_value = mock_old_fp
        mock_pcb.add_footprint.return_value = mock_new_fp

        with (
            patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb),
            patch("kicad_tools.sync.reconciler.shutil.copy2") as mock_copy,
        ):
            reconciler.apply(analysis, dry_run=False)

        mock_copy.assert_called_once()
        backup_call = mock_copy.call_args
        assert str(backup_call[0][1]).endswith(".kicad_pcb.bak")

        Path(sch_path).unlink()
        Path(pcb_path).unlink()


class TestUpdateFootprintSwapRegression:
    """Regression test for duplicate-reference bug during footprint swap.

    When add_footprint and remove_footprint both use the same reference,
    remove_footprint filters _footprints by name and deletes ALL matching
    entries -- including the just-added new footprint.  The fix uses a
    temporary reference for the new footprint, removes the old one, then
    renames the new one to the correct reference.
    """

    def test_remove_does_not_delete_new_footprint(self, tmp_path):
        """Verify that the swap keeps exactly one footprint with the target ref.

        Uses a real PCB._footprints list (not mocks) to prove that the
        temporary-reference strategy avoids the accidental double-delete.
        """
        from kicad_tools.schema.pcb import PCB

        # Load a real PCB with one footprint (R1)
        from tests.conftest import MINIMAL_PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        pcb = PCB.load(str(pcb_file))

        # Sanity: we start with exactly one footprint "R1"
        assert pcb.get_footprint("R1") is not None
        assert len([fp for fp in pcb.footprints if fp.reference == "R1"]) == 1

        # Simulate the temp-ref swap pattern used by _apply_update_footprint:
        # 1. Add new footprint with a temporary reference
        temp_ref = "__SWAP_TEMP__R1"
        # We cannot call add_footprint (needs KiCad libs), so replicate the
        # critical _footprints bookkeeping directly.
        from kicad_tools.schema.pcb import Footprint

        new_fp = Footprint(
            name="Resistor_SMD:R_0805_2012Metric",
            layer="F.Cu",
            position=(50.0, 50.0),
            rotation=0.0,
            reference=temp_ref,
            value="10k",
        )
        pcb._footprints.append(new_fp)

        # 2. Remove old footprint by its real reference
        removed = pcb.remove_footprint("R1")
        assert removed is True

        # 3. The new footprint (temp_ref) must still be present
        remaining = [fp for fp in pcb._footprints if fp.reference == temp_ref]
        assert len(remaining) == 1, (
            "Temporary-reference footprint was incorrectly removed by remove_footprint('R1')"
        )

        # 4. No footprint with the original reference should remain
        assert pcb.get_footprint("R1") is None

        # 5. Rename temp -> real (manually, since the test footprint has no
        #    sexp node -- the real code path uses update_footprint_reference
        #    which handles both sexp and _footprints)
        remaining[0].reference = "R1"
        assert pcb.get_footprint("R1") is not None
        assert pcb.get_footprint("R1") is remaining[0]

    def test_old_pattern_would_delete_both(self, tmp_path):
        """Demonstrate that using the SAME reference for add then remove loses
        the new footprint -- the exact bug this fix prevents."""
        from kicad_tools.schema.pcb import PCB, Footprint
        from tests.conftest import MINIMAL_PCB

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        pcb = PCB.load(str(pcb_file))

        # Simulate the OLD (broken) pattern: add new fp with the SAME ref
        new_fp = Footprint(
            name="Resistor_SMD:R_0805_2012Metric",
            layer="F.Cu",
            position=(50.0, 50.0),
            rotation=0.0,
            reference="R1",
            value="10k",
        )
        pcb._footprints.append(new_fp)

        # Both footprints now have reference "R1"
        assert len([fp for fp in pcb._footprints if fp.reference == "R1"]) == 2

        # remove_footprint("R1") wipes ALL of them from _footprints
        pcb.remove_footprint("R1")

        r1_remaining = [fp for fp in pcb._footprints if fp.reference == "R1"]
        assert len(r1_remaining) == 0, (
            "This test documents the bug: remove_footprint deletes ALL "
            "footprints sharing the same reference, including the new one."
        )


class TestReconcilerApplyUpdateValueViaPCBAPI:
    """Tests for update_value routing through PCB.update_footprint_value()."""

    def _make_reconciler(self):
        """Create a Reconciler with temp file paths."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)
        return reconciler, sch_path, pcb_path

    def test_update_value_calls_pcb_api(self):
        """Test that update_value uses pcb.update_footprint_value() instead of raw sexp."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_value",
                            "reference": "C1",
                            "old_value": "2.2nF 50V",
                            "new_value": "100nF 50V",
                        },
                    ),
                ),
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.update_footprint_value.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 1
        assert changes[0].change_type == "update_value"
        assert changes[0].applied is True
        assert changes[0].old_value == "2.2nF 50V"
        assert changes[0].new_value == "100nF 50V"
        # Verify PCB API was called (not raw sexp manipulation)
        mock_pcb.update_footprint_value.assert_called_once_with("C1", "100nF 50V")
        mock_pcb.save.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_update_value_dry_run(self):
        """Test that update_value in dry-run does not call PCB API."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="C1",
                    pcb_ref="C1",
                    confidence="high",
                    match_type="exact",
                    actions=(
                        {
                            "type": "update_value",
                            "reference": "C1",
                            "old_value": "2.2nF",
                            "new_value": "100nF",
                        },
                    ),
                ),
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        mock_pcb.update_footprint_value.assert_not_called()
        mock_pcb.save.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_rename_calls_pcb_api(self):
        """Test that rename uses pcb.update_footprint_reference() instead of raw sexp."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(
            matches=[
                SyncMatch(
                    schematic_ref="R1",
                    pcb_ref="R99",
                    confidence="high",
                    match_type="value_footprint",
                    actions=(
                        {
                            "type": "rename",
                            "reference": "R99",
                            "old_value": "R99",
                            "new_value": "R1",
                        },
                    ),
                ),
            ]
        )

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.update_footprint_reference.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 1
        assert changes[0].change_type == "rename"
        assert changes[0].applied is True
        mock_pcb.update_footprint_reference.assert_called_once_with("R99", "R1")
        mock_pcb.save.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_update_value_footprint_not_found_returns_none(self):
        """Test that update_value returns None when footprint is not found."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")

        action = {
            "type": "update_value",
            "reference": "MISSING",
            "old_value": "10k",
            "new_value": "4.7k",
        }

        mock_pcb = MagicMock()
        mock_pcb.update_footprint_value.return_value = False

        change = reconciler._apply_update_value(mock_pcb, action, dry_run=False)
        assert change is None


class TestReconcilerApplyRemoveOrphans:
    """Tests for orphan removal in Reconciler.apply()."""

    def _make_reconciler(self):
        """Create a Reconciler with temp file paths."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)
        return reconciler, sch_path, pcb_path

    def test_remove_orphans_applied(self):
        """Test that orphans are removed when remove_orphans=True."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["J5", "R11"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.footprint_has_traces.return_value = False
        mock_pcb.remove_footprint.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False, remove_orphans=True)

        assert len(changes) == 2
        assert all(c.change_type == "remove_orphan" for c in changes)
        assert all(c.applied for c in changes)
        assert changes[0].reference == "J5"
        assert changes[1].reference == "R11"
        assert mock_pcb.remove_footprint.call_count == 2
        mock_pcb.save.assert_called_once()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_dry_run(self):
        """Test that dry-run does not remove orphans."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["J5"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.footprint_has_traces.return_value = False

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=True, remove_orphans=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        assert changes[0].change_type == "remove_orphan"
        assert changes[0].new_value == "removed"
        mock_pcb.remove_footprint.assert_not_called()
        mock_pcb.save.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_skips_traced_without_force(self):
        """Test that orphans with traces are skipped without --force."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["R11"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.footprint_has_traces.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False, remove_orphans=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        assert "has traces" in changes[0].new_value
        mock_pcb.remove_footprint.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_force_removes_traced(self):
        """Test that --force removes orphans even with routed traces."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["R11"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.footprint_has_traces.return_value = True
        mock_pcb.remove_footprint.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(
                analysis, dry_run=False, remove_orphans=True, force=True
            )

        assert len(changes) == 1
        assert changes[0].applied is True
        assert "forced" in changes[0].new_value
        mock_pcb.remove_footprint.assert_called_once_with("R11")

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_not_enabled_by_default(self):
        """Test that orphans are NOT removed without remove_orphans=True."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["J5", "R11"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False)

        assert len(changes) == 0
        mock_pcb.remove_footprint.assert_not_called()

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_empty_list_no_error(self):
        """Test that empty orphan list with remove_orphans=True produces no errors."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=[])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=False, remove_orphans=True)

        assert len(changes) == 0

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_remove_orphans_dry_run_traced_shows_skip(self):
        """Test dry-run with traced orphans shows skip message."""
        reconciler, sch_path, pcb_path = self._make_reconciler()

        analysis = SyncAnalysis(pcb_orphans=["R11"])

        mock_pcb = MagicMock()
        mock_pcb.get_board_outline.return_value = []
        mock_pcb.footprint_has_traces.return_value = True

        with patch("kicad_tools.schema.pcb.PCB.load", return_value=mock_pcb):
            changes = reconciler.apply(analysis, dry_run=True, remove_orphans=True)

        assert len(changes) == 1
        assert changes[0].applied is False
        assert "has traces" in changes[0].new_value
        assert "--force" in changes[0].new_value

        Path(sch_path).unlink()
        Path(pcb_path).unlink()


class TestReconcilerSaveMapping:
    """Tests for Reconciler.save_mapping()."""

    def test_save_mapping_creates_valid_json(self):
        """Test that save_mapping creates a valid JSON file."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path(sch_path)
        reconciler._pcb_path = Path(pcb_path)

        analysis = SyncAnalysis(
            matches=[SyncMatch("R1", "R1", "high", "exact")],
            schematic_orphans=["R5"],
        )

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as out:
            output_path = out.name

        reconciler.save_mapping(analysis, output_path)

        with open(output_path) as f:
            data = json.load(f)

        assert "matches" in data
        assert "schematic_orphans" in data
        assert data["schematic"] == sch_path
        assert data["pcb"] == pcb_path

        Path(sch_path).unlink()
        Path(pcb_path).unlink()
        Path(output_path).unlink()


class TestSmartPlacement:
    """Tests for net-adjacency-based proximity placement of new components."""

    def _make_reconciler(self):
        """Create a Reconciler instance without file validation."""
        reconciler = Reconciler.__new__(Reconciler)
        reconciler._schematic_path = Path("/tmp/test.kicad_sch")
        reconciler._pcb_path = Path("/tmp/test.kicad_pcb")
        return reconciler

    def _make_mock_pcb(self, footprints=None, board_origin=(0.0, 0.0), outline=None):
        """Create a mock PCB with the given footprints."""
        mock_pcb = MagicMock()
        mock_pcb.board_origin = board_origin

        if footprints is None:
            footprints = []
        mock_fps = []
        for ref, x, y in footprints:
            fp = MagicMock()
            fp.reference = ref
            fp.position = (x, y)
            mock_fps.append(fp)
        mock_pcb.footprints = mock_fps
        mock_pcb.get_board_outline.return_value = outline or []
        return mock_pcb

    def test_find_non_overlapping_clear_centroid(self):
        """When centroid is clear, it should be returned directly."""
        x, y = Reconciler._find_non_overlapping_position(10.0, 20.0, [], 5.0)
        assert x == 10.0
        assert y == 20.0

    def test_find_non_overlapping_occupied_centroid(self):
        """When centroid is occupied, a nearby clear position should be found."""
        occupied = [(10.0, 20.0)]
        x, y = Reconciler._find_non_overlapping_position(10.0, 20.0, occupied, 5.0)
        dist = math.sqrt((x - 10.0) ** 2 + (y - 20.0) ** 2)
        # Must be at least min_spacing away from the occupied position
        assert dist >= 5.0
        # But should be close -- within the first ring (radius = 5.0)
        assert dist <= 6.0

    def test_find_non_overlapping_multiple_occupied(self):
        """Position found should be clear of all occupied positions."""
        occupied = [(10.0, 20.0), (15.0, 20.0), (10.0, 25.0)]
        x, y = Reconciler._find_non_overlapping_position(10.0, 20.0, occupied, 5.0)
        for ox, oy in occupied:
            dist = math.sqrt((x - ox) ** 2 + (y - oy) ** 2)
            assert dist >= 5.0

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_build_net_adjacency_basic(self, mock_export):
        """Components sharing nets should appear as neighbors."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        # U1 and C1 share VCC net, U1 and R1 share SIG net
        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="C1", pin="1"),
                NetNode(reference="C2", pin="1"),
            ]),
            NetlistNet(code=2, name="GND", nodes=[
                NetNode(reference="U1", pin="2"),
                NetNode(reference="C1", pin="2"),
                NetNode(reference="C2", pin="2"),
            ]),
            NetlistNet(code=3, name="SIG", nodes=[
                NetNode(reference="U1", pin="3"),
                NetNode(reference="R1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        # C1 and R1 are new; U1 and C2 are existing
        adjacency = reconciler._build_net_adjacency({"C1", "R1"})

        # C1 should have U1 and C2 as neighbors (shares VCC and GND)
        assert "U1" in adjacency["C1"]
        assert "C2" in adjacency["C1"]
        # R1 should have U1 as neighbor (shares SIG)
        assert "U1" in adjacency["R1"]
        # U1 is not new, so should not be in adjacency
        assert "U1" not in adjacency

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_build_net_adjacency_no_placed_neighbors(self, mock_export):
        """New components sharing nets only with other new components get no neighbors."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="C1", pin="1"),
                NetNode(reference="C2", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        adjacency = reconciler._build_net_adjacency({"C1", "C2"})

        # Both are new, so neither has placed neighbors
        assert adjacency.get("C1", set()) == set()
        assert adjacency.get("C2", set()) == set()

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_compute_smart_placement_near_neighbor(self, mock_export):
        """New component should be placed near its net-neighbor."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="C1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        # U1 is at sheet-absolute (50, 60), board origin is (0, 0)
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 50.0, 60.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1"])

        assert "C1" in positions
        cx, cy = positions["C1"]
        # Should be within 10mm of U1's position (the acceptance criterion)
        dist = math.sqrt((cx - 50.0) ** 2 + (cy - 60.0) ** 2)
        assert dist < 10.0

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_compute_smart_placement_centroid_of_multiple_neighbors(self, mock_export):
        """New component with multiple neighbors should be near their centroid."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="U2", pin="1"),
                NetNode(reference="C1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        # U1 at (20, 30), U2 at (40, 30) -> centroid at (30, 30)
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 20.0, 30.0), ("U2", 40.0, 30.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1"])

        assert "C1" in positions
        cx, cy = positions["C1"]
        # Should be within 10mm of the centroid (30, 30)
        dist = math.sqrt((cx - 30.0) ** 2 + (cy - 30.0) ** 2)
        assert dist < 10.0

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_compute_smart_placement_no_neighbors_returns_empty(self, mock_export):
        """Component with no net-neighbors should not appear in smart positions."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 50.0, 60.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1"])

        # C1 shares no net with any existing component
        assert "C1" not in positions

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_compute_smart_placement_with_board_origin(self, mock_export):
        """Smart placement should account for board origin offset."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="C1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        # U1 at sheet-absolute (150, 160), board origin at (100, 100)
        # -> board-relative position is (50, 60)
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 150.0, 160.0)],
            board_origin=(100.0, 100.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1"])

        assert "C1" in positions
        cx, cy = positions["C1"]
        # Board-relative centroid should be near (50, 60)
        dist = math.sqrt((cx - 50.0) ** 2 + (cy - 60.0) ** 2)
        assert dist < 10.0

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_smart_placement_no_overlap(self, mock_export):
        """Multiple new components should not overlap each other."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="C1", pin="1"),
                NetNode(reference="C2", pin="1"),
                NetNode(reference="C3", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 50.0, 50.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1", "C2", "C3"])

        assert len(positions) == 3
        # Verify no two new positions are too close (min_spacing = 5.0)
        placed = list(positions.values())
        for i in range(len(placed)):
            for j in range(i + 1, len(placed)):
                dist = math.sqrt(
                    (placed[i][0] - placed[j][0]) ** 2
                    + (placed[i][1] - placed[j][1]) ** 2
                )
                assert dist >= 5.0, (
                    f"Components too close: {placed[i]} and {placed[j]}, dist={dist}"
                )

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_decoupling_cap_placed_near_ic(self, mock_export):
        """Decoupling capacitor for an IC should be placed adjacent to it."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        # IC U1 and decoupling cap C1 share VCC and GND nets
        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="14"),
                NetNode(reference="C1", pin="1"),
            ]),
            NetlistNet(code=2, name="GND", nodes=[
                NetNode(reference="U1", pin="7"),
                NetNode(reference="C1", pin="2"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 80.0, 80.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1"])

        assert "C1" in positions
        cx, cy = positions["C1"]
        dist = math.sqrt((cx - 80.0) ** 2 + (cy - 80.0) ** 2)
        # Decoupling cap must be within 10mm of the IC (acceptance criterion)
        assert dist < 10.0

    @patch("kicad_tools.operations.netlist.export_netlist")
    def test_grid_fallback_for_orphan_components(self, mock_export):
        """Components with no net-neighbors should fall back to grid placement."""
        from kicad_tools.operations.netlist import NetlistNet, NetNode

        # C1 shares a net with U1, C2 is isolated (no shared nets)
        mock_netlist = MagicMock()
        mock_netlist.nets = [
            NetlistNet(code=1, name="VCC", nodes=[
                NetNode(reference="U1", pin="1"),
                NetNode(reference="C1", pin="1"),
            ]),
        ]
        mock_export.return_value = mock_netlist

        reconciler = self._make_reconciler()
        mock_pcb = self._make_mock_pcb(
            footprints=[("U1", 50.0, 50.0)],
            board_origin=(0.0, 0.0),
        )

        positions = reconciler._compute_smart_placement(mock_pcb, ["C1", "C2"])

        # C1 should get smart placement
        assert "C1" in positions
        # C2 has no neighbors, so it should not appear (grid fallback in apply())
        assert "C2" not in positions


class TestComputePlacementStart:
    """Tests for Reconciler._compute_placement_start coordinate handling."""

    def test_nonzero_origin_no_double_subtraction(self, tmp_path):
        """Placement start must use board-relative coords directly.

        get_board_outline() returns board-relative coordinates, so
        _compute_placement_start must NOT subtract the origin again.
        """
        mock_pcb = MagicMock()
        mock_pcb.board_origin = (100.0, 80.0)
        # Board-relative outline (already transformed by get_board_outline)
        mock_pcb.get_board_outline.return_value = [
            (0, 0), (50, 0), (50, 30), (0, 30),
        ]

        # Create minimal files so Reconciler can be instantiated
        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text("(kicad_sch (version 20231120) (generator test) (uuid x))")
        pcb.write_text("(kicad_pcb (version 20240108) (generator test))")

        reconciler = Reconciler(schematic=str(sch), pcb=str(pcb))
        x, y, col = reconciler._compute_placement_start(mock_pcb)

        assert x == pytest.approx(0.0)
        assert y == pytest.approx(40.0)
        assert col == 0

    def test_no_outline_returns_defaults(self, tmp_path):
        """Without outline, placement defaults to (10, 10)."""
        mock_pcb = MagicMock()
        mock_pcb.board_origin = (0.0, 0.0)
        mock_pcb.get_board_outline.return_value = []

        sch = tmp_path / "test.kicad_sch"
        pcb = tmp_path / "test.kicad_pcb"
        sch.write_text("(kicad_sch (version 20231120) (generator test) (uuid x))")
        pcb.write_text("(kicad_pcb (version 20240108) (generator test))")

        reconciler = Reconciler(schematic=str(sch), pcb=str(pcb))
        x, y, col = reconciler._compute_placement_start(mock_pcb)

        assert x == pytest.approx(10.0)
        assert y == pytest.approx(10.0)
        assert col == 0
