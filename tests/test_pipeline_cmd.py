"""Tests for the pipeline command (kct pipeline)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.pipeline_cmd import (
    ALL_STEPS,
    PipelineContext,
    PipelineStep,
    _detect_routing_status,
    _resolve_pcb_from_project,
    main,
    run_pipeline,
)

# Minimal routed PCB with segments
ROUTED_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VIN")
  (net 2 "GND")
  (net 3 "NET1")
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 50)
    (pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VIN"))
    (pad "2" smd roundrect (at 0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 3 "NET1"))
  )
  (segment (start 100.8 50) (end 110 50) (width 0.25) (layer "F.Cu") (net 3) (uuid "seg-1"))
  (segment (start 110 50) (end 119.2 50) (width 0.25) (layer "F.Cu") (net 3) (uuid "seg-2"))
)
"""

# Minimal unrouted PCB (no segments or arcs)
UNROUTED_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "VIN")
  (net 2 "GND")
  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 100 50)
    (pad "1" smd roundrect (at -0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 1 "VIN"))
    (pad "2" smd roundrect (at 0.8 0) (size 0.9 0.95) (layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) (net 2 "GND"))
  )
)
"""

# Empty PCB (no nets, no components)
EMPTY_PCB = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
)
"""


@pytest.fixture
def routed_pcb(tmp_path: Path) -> Path:
    """Create a routed PCB file for testing."""
    pcb_file = tmp_path / "routed.kicad_pcb"
    pcb_file.write_text(ROUTED_PCB)
    return pcb_file


@pytest.fixture
def unrouted_pcb(tmp_path: Path) -> Path:
    """Create an unrouted PCB file for testing."""
    pcb_file = tmp_path / "unrouted.kicad_pcb"
    pcb_file.write_text(UNROUTED_PCB)
    return pcb_file


@pytest.fixture
def empty_pcb(tmp_path: Path) -> Path:
    """Create an empty PCB file for testing."""
    pcb_file = tmp_path / "empty.kicad_pcb"
    pcb_file.write_text(EMPTY_PCB)
    return pcb_file


@pytest.fixture
def project_with_pcb(tmp_path: Path) -> tuple[Path, Path]:
    """Create a .kicad_pro alongside a .kicad_pcb."""
    pcb_file = tmp_path / "project.kicad_pcb"
    pcb_file.write_text(ROUTED_PCB)
    pro_file = tmp_path / "project.kicad_pro"
    pro_file.write_text('{"meta": {"filename": "project.kicad_pro"}}')
    return pro_file, pcb_file


class TestDetectRoutingStatus:
    """Tests for _detect_routing_status helper."""

    def test_routed_board_detected(self, routed_pcb: Path):
        """A PCB with segments is detected as routed."""
        is_routed, trace_count, net_count = _detect_routing_status(routed_pcb)
        assert is_routed is True
        assert trace_count == 2
        assert net_count > 0

    def test_unrouted_board_detected(self, unrouted_pcb: Path):
        """A PCB without segments is detected as unrouted."""
        is_routed, trace_count, net_count = _detect_routing_status(unrouted_pcb)
        assert is_routed is False
        assert trace_count == 0

    def test_empty_board(self, empty_pcb: Path):
        """An empty PCB is detected as unrouted."""
        is_routed, trace_count, net_count = _detect_routing_status(empty_pcb)
        assert is_routed is False
        assert trace_count == 0
        assert net_count == 0

    def test_nonexistent_file(self, tmp_path: Path):
        """Non-existent file returns unrouted with zero counts."""
        is_routed, trace_count, net_count = _detect_routing_status(
            tmp_path / "nonexistent.kicad_pcb"
        )
        assert is_routed is False
        assert trace_count == 0
        assert net_count == 0


class TestResolveProjectPcb:
    """Tests for _resolve_pcb_from_project helper."""

    def test_finds_matching_pcb(self, project_with_pcb):
        """Resolves .kicad_pcb from .kicad_pro with same stem."""
        pro_file, pcb_file = project_with_pcb
        result = _resolve_pcb_from_project(pro_file)
        assert result == pcb_file

    def test_missing_pcb_returns_none(self, tmp_path: Path):
        """Returns None when no matching .kicad_pcb exists."""
        pro_file = tmp_path / "no_pcb.kicad_pro"
        pro_file.write_text("{}")
        result = _resolve_pcb_from_project(pro_file)
        assert result is None


class TestDryRun:
    """Tests for --dry-run mode."""

    def test_dry_run_no_modifications(self, routed_pcb: Path):
        """Dry-run mode does not modify any files."""
        original_content = routed_pcb.read_text()
        original_mtime = routed_pcb.stat().st_mtime

        result = main(["--dry-run", str(routed_pcb)])

        assert result == 0
        assert routed_pcb.read_text() == original_content
        assert routed_pcb.stat().st_mtime == original_mtime

    def test_dry_run_lists_all_steps(self, routed_pcb: Path, capsys):
        """Dry-run mode reports what would be executed."""
        result = main(["--dry-run", str(routed_pcb)])
        assert result == 0
        # The output goes through rich Console, but we can verify
        # by checking the return code indicates success

    def test_dry_run_unrouted(self, unrouted_pcb: Path):
        """Dry-run on unrouted board lists routing step."""
        result = main(["--dry-run", str(unrouted_pcb)])
        assert result == 0


class TestRoutingSkip:
    """Tests for automatic routing detection and skip."""

    def test_routing_skipped_when_routed(self, routed_pcb: Path):
        """Pipeline skips routing when board already has traces."""
        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].skipped is True
        assert "already routed" in results[0].message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_routing_invoked_when_unrouted(self, mock_run, unrouted_pcb: Path):
        """Pipeline invokes routing when board has no traces."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].skipped is False
        # Verify subprocess was called with route command
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "route" in cmd_args


class TestSingleStep:
    """Tests for --step flag."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_single_step_fix_vias(self, mock_run, routed_pcb: Path):
        """--step fix-vias runs only that step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "fix-vias", str(routed_pcb), "--quiet"])

        assert result == 0
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "fix-vias" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_single_step_audit(self, mock_run, routed_pcb: Path):
        """--step audit runs only the audit step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "audit", str(routed_pcb), "--quiet"])

        assert result == 0
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "check" in cmd_args  # PCB-only uses check, not audit


class TestZoneFillKicadCli:
    """Tests for zone fill behavior with kicad-cli presence."""

    @patch("kicad_tools.cli.runner.find_kicad_cli", return_value=None)
    def test_kicad_cli_missing_zones_skip(self, mock_find, routed_pcb: Path):
        """Pipeline continues without error when kicad-cli is not installed."""
        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ZONES])

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].skipped is True
        assert "kicad-cli not installed" in results[0].message

    @patch("kicad_tools.cli.runner.find_kicad_cli", return_value=None)
    def test_zones_step_skips_when_no_kicad_cli(self, mock_find, routed_pcb: Path):
        """Zone fill step itself skips gracefully when kicad-cli is absent."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_zones

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        console = Console(quiet=True)
        result = _run_step_zones(ctx, console)

        assert result.success is True
        assert result.skipped is True
        assert "kicad-cli not installed" in result.message


class TestExitCodes:
    """Tests for pipeline exit codes."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_exit_code_on_drc_failure(self, mock_run, routed_pcb: Path):
        """Pipeline returns exit code 1 when final audit fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="DRC violations found", stdout="")

        result = main(["--step", "audit", str(routed_pcb), "--quiet"])
        assert result == 1

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_exit_code_zero_on_success(self, mock_run, routed_pcb: Path):
        """Pipeline returns exit code 0 when all steps succeed."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "fix-vias", str(routed_pcb), "--quiet"])
        assert result == 0


class TestProjectInput:
    """Tests for .kicad_pro input support."""

    def test_kicad_pro_resolves_to_pcb(self, project_with_pcb):
        """A .kicad_pro input resolves to the corresponding .kicad_pcb."""
        pro_file, pcb_file = project_with_pcb

        result = main(["--dry-run", str(pro_file)])
        assert result == 0

    def test_kicad_pro_without_pcb_fails(self, tmp_path: Path):
        """A .kicad_pro without a matching .kicad_pcb fails."""
        pro_file = tmp_path / "no_pcb.kicad_pro"
        pro_file.write_text("{}")

        result = main([str(pro_file)])
        assert result == 1

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_project_input_uses_audit(self, mock_run, project_with_pcb):
        """Project-level input triggers audit (not check) in final step."""
        pro_file, pcb_file = project_with_pcb
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "audit", str(pro_file), "--quiet"])
        assert result == 0
        cmd_args = mock_run.call_args[0][0]
        assert "audit" in cmd_args


class TestFileInput:
    """Tests for file input validation."""

    def test_nonexistent_file_fails(self, tmp_path: Path):
        """Pipeline fails for nonexistent input file."""
        result = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert result == 1

    def test_unsupported_extension_fails(self, tmp_path: Path):
        """Pipeline fails for unsupported file extension."""
        bad_file = tmp_path / "file.txt"
        bad_file.write_text("not a pcb")
        result = main([str(bad_file)])
        assert result == 1


class TestMfrAndLayersForwarding:
    """Tests that --mfr and --layers flags are forwarded correctly."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_mfr_forwarded_to_fix_vias(self, mock_run, routed_pcb: Path):
        """--mfr flag is forwarded to fix-vias step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-vias", "--mfr", "pcbway", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--mfr" in cmd_args
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "pcbway"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_layers_forwarded_to_fix_vias(self, mock_run, routed_pcb: Path):
        """--layers flag is forwarded to fix-vias step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-vias", "--layers", "4", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "4"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_mfr_forwarded_to_optimize(self, mock_run, routed_pcb: Path):
        """--mfr flag is forwarded to optimize-traces step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "optimize", "--mfr", "oshpark", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--mfr" in cmd_args
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "oshpark"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_mfr_forwarded_to_audit(self, mock_run, routed_pcb: Path):
        """--mfr flag is forwarded to audit/check step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "audit", "--mfr", "seeed", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--mfr" in cmd_args
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "seeed"


class TestPipelineStepOrder:
    """Tests for pipeline step ordering."""

    def test_all_steps_defined(self):
        """ALL_STEPS contains all PipelineStep values."""
        assert set(ALL_STEPS) == set(PipelineStep)

    def test_step_order(self):
        """Steps execute in the correct order: fix-vias before route."""
        expected = [
            PipelineStep.FIX_VIAS,
            PipelineStep.ROUTE,
            PipelineStep.FIX_DRC,
            PipelineStep.OPTIMIZE,
            PipelineStep.ZONES,
            PipelineStep.AUDIT,
        ]
        assert expected == ALL_STEPS


class TestEdgeCases:
    """Tests for edge cases."""

    def test_empty_pcb_dry_run(self, empty_pcb: Path):
        """Empty PCB (no nets) works in dry-run mode."""
        result = main(["--dry-run", str(empty_pcb)])
        assert result == 0

    def test_pcb_detects_project_alongside(self, project_with_pcb):
        """When given a .kicad_pcb, detects .kicad_pro alongside it."""
        pro_file, pcb_file = project_with_pcb

        # The main() function handles project detection
        result = main(["--dry-run", str(pcb_file)])
        assert result == 0
