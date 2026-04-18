"""CLI integration tests for kct sync command."""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.sync_cmd import main as sync_main


class TestSyncCLIArgParsing:
    """Tests for sync command argument parsing."""

    def test_no_mode_flag_fails(self):
        """Test that omitting --analyze/--apply fails."""
        with pytest.raises(SystemExit) as exc_info:
            sync_main(["project.kicad_pro"])
        assert exc_info.value.code != 0

    def test_analyze_mode_requires_project(self):
        """Test that --analyze without files reports error."""
        result = sync_main(["--analyze"])
        assert result == 1

    def test_apply_without_dry_run_or_confirm_fails(self):
        """Test that --apply without --dry-run or --confirm fails."""
        result = sync_main(["--apply", "project.kicad_pro"])
        assert result == 1

    def test_apply_with_dry_run_accepted(self):
        """Test that --apply --dry-run is accepted (file missing is OK error)."""
        result = sync_main(["--apply", "--dry-run", "/nonexistent.kicad_pro"])
        assert result == 1  # file not found, but arg parsing succeeded

    def test_apply_with_confirm_accepted(self):
        """Test that --apply --confirm is accepted (file missing is OK error)."""
        result = sync_main(["--apply", "--confirm", "/nonexistent.kicad_pro"])
        assert result == 1  # file not found, but arg parsing succeeded


class TestSyncCLIAnalyzeMode:
    """Tests for sync CLI --analyze mode with mocked reconciler."""

    def _mock_reconciler(self, analysis):
        """Create a mock Reconciler that returns the given analysis."""
        mock = MagicMock()
        mock.analyze.return_value = analysis
        return mock

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_analyze_in_sync_returns_0(self, MockReconciler, capsys):
        """Test that in-sync analysis returns exit code 0."""
        from kicad_tools.sync.reconciler import SyncAnalysis

        analysis = SyncAnalysis()  # empty = in sync
        MockReconciler.return_value = self._mock_reconciler(analysis)

        result = sync_main(["--analyze", "project.kicad_pro"])
        assert result == 0
        captured = capsys.readouterr()
        assert "IN SYNC" in captured.out

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_analyze_out_of_sync_returns_2(self, MockReconciler):
        """Test that out-of-sync analysis returns exit code 2."""
        from kicad_tools.sync.reconciler import SyncAnalysis

        analysis = SyncAnalysis(schematic_orphans=["R5"])
        MockReconciler.return_value = self._mock_reconciler(analysis)

        result = sync_main(["--analyze", "project.kicad_pro"])
        assert result == 2

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_analyze_json_format(self, MockReconciler, capsys):
        """Test JSON output format for --analyze."""
        from kicad_tools.sync.reconciler import SyncAnalysis, SyncMatch

        analysis = SyncAnalysis(
            matches=[SyncMatch("R1", "R1", "high", "exact")],
            schematic_orphans=["R5"],
        )
        MockReconciler.return_value = self._mock_reconciler(analysis)

        result = sync_main(["--analyze", "--format", "json", "project.kicad_pro"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "matches" in data
        assert data["schematic_orphans"] == ["R5"]

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_analyze_table_format_shows_orphans(self, MockReconciler, capsys):
        """Test table output shows orphan details."""
        from kicad_tools.sync.reconciler import SyncAnalysis

        analysis = SyncAnalysis(
            schematic_orphans=["R5"],
            pcb_orphans=["C3"],
        )
        MockReconciler.return_value = self._mock_reconciler(analysis)

        sync_main(["--analyze", "project.kicad_pro"])
        captured = capsys.readouterr()
        assert "R5" in captured.out
        assert "C3" in captured.out
        assert "SCHEMATIC-ONLY" in captured.out
        assert "PCB-ONLY" in captured.out


class TestSyncCLIApplyMode:
    """Tests for sync CLI --apply mode with mocked reconciler."""

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_apply_dry_run_shows_changes(self, MockReconciler, capsys):
        """Test --apply --dry-run shows proposed changes."""
        from kicad_tools.sync.reconciler import SyncAnalysis, SyncChange

        analysis = SyncAnalysis()
        mock = MagicMock()
        mock.analyze.return_value = analysis
        mock.apply.return_value = [
            SyncChange("R1", "update_value", "10k", "4.7k", applied=False),
        ]
        MockReconciler.return_value = mock

        result = sync_main(["--apply", "--dry-run", "project.kicad_pro"])
        assert result == 0
        captured = capsys.readouterr()
        assert "DRY RUN" in captured.out
        assert "R1" in captured.out

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_apply_confirm_applies_changes(self, MockReconciler, capsys):
        """Test --apply --confirm applies and reports changes."""
        from kicad_tools.sync.reconciler import SyncAnalysis, SyncChange

        analysis = SyncAnalysis()
        mock = MagicMock()
        mock.analyze.return_value = analysis
        mock.apply.return_value = [
            SyncChange("R1", "update_value", "10k", "4.7k", applied=True),
        ]
        MockReconciler.return_value = mock

        result = sync_main(["--apply", "--confirm", "project.kicad_pro"])
        assert result == 0
        captured = capsys.readouterr()
        assert "APPLIED" in captured.out

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_apply_no_changes(self, MockReconciler, capsys):
        """Test --apply with no changes to apply."""
        from kicad_tools.sync.reconciler import SyncAnalysis

        analysis = SyncAnalysis()
        mock = MagicMock()
        mock.analyze.return_value = analysis
        mock.apply.return_value = []
        MockReconciler.return_value = mock

        result = sync_main(["--apply", "--dry-run", "project.kicad_pro"])
        assert result == 0
        captured = capsys.readouterr()
        assert "No changes" in captured.out

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_apply_json_format(self, MockReconciler, capsys):
        """Test JSON output for --apply."""
        from kicad_tools.sync.reconciler import SyncAnalysis, SyncChange

        analysis = SyncAnalysis()
        mock = MagicMock()
        mock.analyze.return_value = analysis
        mock.apply.return_value = [
            SyncChange("R1", "rename", "R99", "R1", applied=False),
        ]
        MockReconciler.return_value = mock

        result = sync_main(["--apply", "--dry-run", "--format", "json", "project.kicad_pro"])
        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert len(data["changes"]) == 1


class TestSyncCLIMapping:
    """Tests for --output-mapping flag."""

    @patch("kicad_tools.sync.reconciler.Reconciler")
    def test_output_mapping_saves_file(self, MockReconciler, capsys, tmp_path):
        """Test that --output-mapping saves a JSON file."""
        from kicad_tools.sync.reconciler import SyncAnalysis

        analysis = SyncAnalysis()
        mock = MagicMock()
        mock.analyze.return_value = analysis
        MockReconciler.return_value = mock

        mapping_path = str(tmp_path / "mapping.json")
        result = sync_main(
            ["--analyze", "--output-mapping", mapping_path, "project.kicad_pro"]
        )
        assert result == 0

        # Verify the mapping call was made
        mock.save_mapping.assert_called_once()
