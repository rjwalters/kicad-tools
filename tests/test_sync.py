"""Tests for kicad_tools.sync.reconciler module."""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

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
            value_mismatches=[
                {"reference": "R1", "schematic_value": "10k", "pcb_value": "4.7k"}
            ]
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

    def _make_mock_symbol(self, ref: str, value: str = "", footprint: str = ""):
        """Create a mock schematic symbol."""
        sym = MagicMock()
        sym.reference = ref
        sym.value = value
        sym.footprint = footprint
        return sym

    def _make_mock_footprint(self, ref: str, value: str = "", name: str = ""):
        """Create a mock PCB footprint."""
        fp = MagicMock()
        fp.reference = ref
        fp.value = value
        fp.name = name
        fp.pads = []
        return fp

    def _make_reconciler_with_mocks(self, symbols, footprints):
        """Create a Reconciler with mocked file loading."""
        with tempfile.NamedTemporaryFile(suffix=".kicad_sch", delete=False) as sf:
            sch_path = sf.name
        with tempfile.NamedTemporaryFile(suffix=".kicad_pcb", delete=False) as pf:
            pcb_path = pf.name

        mock_sch = MagicMock()
        mock_sch.symbols = symbols
        mock_pcb = MagicMock()
        mock_pcb.footprints = footprints

        with patch("kicad_tools.validate.consistency.SchematicPCBChecker.__init__", return_value=None):
            reconciler = Reconciler.__new__(Reconciler)
            reconciler._schematic_path = Path(sch_path)
            reconciler._pcb_path = Path(pcb_path)

        return reconciler, mock_sch, mock_pcb, sch_path, pcb_path

    def test_analyze_in_sync(self):
        """Test analysis when schematic and PCB are in sync."""
        symbols = [self._make_mock_symbol("R1", "10k", "Resistor_SMD:R_0402")]
        footprints = [self._make_mock_footprint("R1", "10k", "Resistor_SMD:R_0402")]

        reconciler, mock_sch, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            symbols, footprints
        )

        mock_checker = MagicMock()
        mock_checker.schematic = mock_sch
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with patch(
            "kicad_tools.validate.consistency.SchematicPCBChecker",
            return_value=mock_checker,
        ):
            analysis = reconciler.analyze()

        assert analysis.is_in_sync
        assert len(analysis.matches) == 1
        assert analysis.matches[0].confidence == "high"
        assert len(analysis.schematic_orphans) == 0
        assert len(analysis.pcb_orphans) == 0

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_value_mismatch(self):
        """Test analysis detects value mismatches."""
        symbols = [self._make_mock_symbol("R1", "10k", "Resistor_SMD:R_0402")]
        footprints = [self._make_mock_footprint("R1", "4.7k", "Resistor_SMD:R_0402")]

        reconciler, mock_sch, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            symbols, footprints
        )

        mock_checker = MagicMock()
        mock_checker.schematic = mock_sch
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with patch(
            "kicad_tools.validate.consistency.SchematicPCBChecker",
            return_value=mock_checker,
        ):
            analysis = reconciler.analyze()

        assert not analysis.is_in_sync
        assert len(analysis.value_mismatches) == 1
        assert analysis.value_mismatches[0]["schematic_value"] == "10k"
        assert analysis.value_mismatches[0]["pcb_value"] == "4.7k"

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_orphans(self):
        """Test analysis detects orphans in both directions."""
        symbols = [
            self._make_mock_symbol("R1", "10k", "R_0402"),
            self._make_mock_symbol("R2", "22k", "R_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
            self._make_mock_footprint("C1", "100nF", "C_0402"),
        ]

        reconciler, mock_sch, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            symbols, footprints
        )

        mock_checker = MagicMock()
        mock_checker.schematic = mock_sch
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with patch(
            "kicad_tools.validate.consistency.SchematicPCBChecker",
            return_value=mock_checker,
        ):
            analysis = reconciler.analyze()

        assert "R2" in analysis.schematic_orphans
        assert "C1" in analysis.pcb_orphans

        Path(sch_path).unlink()
        Path(pcb_path).unlink()

    def test_analyze_medium_confidence_match(self):
        """Test that value+footprint matching produces medium confidence."""
        symbols = [
            self._make_mock_symbol("R1", "10k", "Resistor_SMD:R_0402"),
            self._make_mock_symbol("R5", "22k", "Resistor_SMD:R_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "Resistor_SMD:R_0402"),
            self._make_mock_footprint("R99", "22k", "Resistor_SMD:R_0402"),
        ]

        reconciler, mock_sch, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            symbols, footprints
        )

        mock_checker = MagicMock()
        mock_checker.schematic = mock_sch
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with patch(
            "kicad_tools.validate.consistency.SchematicPCBChecker",
            return_value=mock_checker,
        ):
            analysis = reconciler.analyze()

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
        symbols = [
            self._make_mock_symbol("R1", "10k", "R_0402"),
            self._make_mock_symbol("C1", "100nF", "C_0402"),
        ]
        footprints = [
            self._make_mock_footprint("R1", "10k", "R_0402"),
            self._make_mock_footprint("C1", "100nF", "C_0402"),
        ]

        reconciler, mock_sch, mock_pcb, sch_path, pcb_path = self._make_reconciler_with_mocks(
            symbols, footprints
        )

        mock_checker = MagicMock()
        mock_checker.schematic = mock_sch
        mock_checker.pcb = mock_pcb
        mock_checker.check.return_value = MagicMock(issues=[])

        with patch(
            "kicad_tools.validate.consistency.SchematicPCBChecker",
            return_value=mock_checker,
        ):
            analysis = reconciler.analyze()

        assert analysis.is_in_sync

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
