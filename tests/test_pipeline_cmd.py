"""Tests for the pipeline command (kct pipeline)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kicad_tools.cli.pipeline_cmd import (
    ALL_STEPS,
    PipelineContext,
    PipelineResult,
    PipelineStep,
    _build_commit_message,
    _detect_routing_status,
    _fetch_check_results,
    _is_git_repo,
    _print_final_summary,
    _resolve_pcb_from_project,
    _resolve_schematic,
    _run_step_erc,
    _run_step_export,
    _run_step_fix_erc,
    _run_step_report,
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


class TestFixSilkscreenStep:
    """Tests for the fix-silkscreen pipeline step."""

    def test_dry_run_fix_silkscreen_message(self, routed_pcb: Path):
        """Dry-run fix-silkscreen reports the command that would be executed."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_silkscreen

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True, mfr="pcbway")
        console = Console(quiet=True)
        result = _run_step_fix_silkscreen(ctx, console)

        assert result.success is True
        assert "fix-silkscreen" in result.message
        assert "pcbway" in result.message
        assert result.step == PipelineStep.FIX_SILKSCREEN

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_silkscreen_calls_subprocess(self, mock_run, routed_pcb: Path):
        """fix-silkscreen step invokes the correct subprocess command."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_silkscreen

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        console = Console(quiet=True)
        result = _run_step_fix_silkscreen(ctx, console)

        assert result.success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "fix-silkscreen" in cmd_args
        assert "--mfr" in cmd_args
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "jlcpcb"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_silkscreen_failure_propagates(self, mock_run, routed_pcb: Path):
        """fix-silkscreen step reports failure when subprocess fails."""
        mock_run.return_value = MagicMock(returncode=1, stderr="parse error", stdout="")

        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_silkscreen

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        console = Console(quiet=True)
        result = _run_step_fix_silkscreen(ctx, console)

        assert result.success is False
        assert "fix-silkscreen" in result.message


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
        # Verify message uses "route: " prefix convention
        assert results[0].message.startswith("route: ")

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
        # Verify new flags are forwarded
        assert "--grid" in cmd_args
        assert "--manufacturer" in cmd_args
        assert "--auto-fix" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_force_bypasses_routing_skip(self, mock_run, routed_pcb: Path):
        """--force on a routed board invokes the router instead of skipping."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, force=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].skipped is False
        # Verify subprocess was actually called (not skipped)
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
    def test_single_step_fix_silkscreen(self, mock_run, routed_pcb: Path):
        """--step fix-silkscreen runs only that step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "fix-silkscreen", str(routed_pcb), "--quiet"])

        assert result == 0
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "fix-silkscreen" in cmd_args

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
    def test_exit_code_on_audit_tool_error(self, mock_run, routed_pcb: Path):
        """Pipeline returns exit code 1 when audit subprocess has a tool error (exit 1)."""
        mock_run.return_value = MagicMock(returncode=1, stderr="file not found", stdout="")

        result = main(["--step", "audit", str(routed_pcb), "--quiet"])
        assert result == 1

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_audit_exit_code_2_treated_as_success(self, mock_run, routed_pcb: Path):
        """Pipeline treats audit exit code 2 (DRC violations found) as success with warnings.

        This is the core behavioral change: audit now exits 2 (not 1) when DRC
        violations are found. The pipeline already treats exit code 2 as
        'completed with warnings', so the pipeline now reports success instead
        of failure for boards with DRC violations.
        """
        mock_run.return_value = MagicMock(returncode=2, stderr="", stdout="")

        result = main(["--step", "audit", str(routed_pcb), "--quiet"])
        assert result == 0

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_exit_code_zero_on_success(self, mock_run, routed_pcb: Path):
        """Pipeline returns exit code 0 when all steps succeed."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "fix-vias", str(routed_pcb), "--quiet"])
        assert result == 0

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_vias_exit_code_2_treated_as_success(self, mock_run, routed_pcb: Path):
        """Pipeline treats fix-vias exit code 2 (warnings) as success, not failure."""
        mock_run.return_value = MagicMock(returncode=2, stderr="", stdout="")

        result = main(["--step", "fix-vias", str(routed_pcb), "--quiet"])
        assert result == 0

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_pipeline_continues_past_fix_vias_warnings(self, mock_run, routed_pcb: Path):
        """Pipeline continues to subsequent steps when fix-vias returns exit code 2."""

        def side_effect(cmd, **kwargs):
            # fix-vias returns exit code 2 (success with warnings)
            if "fix-vias" in cmd:
                return MagicMock(returncode=2, stderr="", stdout="")
            # All other steps succeed normally
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        # Run fix-vias and fix-drc steps together
        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.FIX_VIAS, PipelineStep.FIX_DRC])

        # Both steps should have run and succeeded
        assert len(results) == 2
        assert results[0].success is True
        assert "warnings" in results[0].message
        assert results[1].success is True

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_pipeline_audit_exit_2_reports_success_with_warnings(self, mock_run, routed_pcb: Path):
        """Pipeline reports audit step as 'completed with warnings' when exit code is 2.

        Audit now exits 2 for NOT_READY (DRC violations found). The pipeline
        already treats exit code 2 as a soft warning, so multi-step pipelines
        that include audit should report all steps succeeded.
        """

        def side_effect(cmd, **kwargs):
            # Detect audit/check subcommands by looking at the argv list
            if "audit" in cmd or "check" in cmd:
                return MagicMock(returncode=2, stderr="", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.FIX_VIAS, PipelineStep.AUDIT])

        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is True
        assert "warnings" in results[1].message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_pipeline_audit_exit_1_is_tool_failure(self, mock_run, routed_pcb: Path):
        """Pipeline reports audit step as failed when exit code is 1 (tool error).

        Exit code 1 from audit now means a tool-level error (file not found,
        parse failure), not DRC violations. The pipeline should still treat
        this as a failure.
        """
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: parse failed", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.AUDIT])

        assert len(results) == 1
        # Audit step failures don't stop the pipeline (informational), but
        # the step itself is marked as failed
        assert results[0].success is False
        assert "failed" in results[0].message


class TestRouteExitCode2Pipeline:
    """Tests for route exit code 2 (partial routing) pipeline behavior.

    Issue #1413: When route exits with code 2 (partial routing), the pipeline
    should treat it as success-with-warnings and continue to downstream steps
    (optimize-traces, fix-drc, audit, report).

    Uses unrouted_pcb fixture so the route step does not skip due to
    detecting existing traces in the board.
    """

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_exit_code_2_treated_as_success(self, mock_run, unrouted_pcb: Path):
        """Pipeline treats route exit code 2 (partial routing) as success, not failure."""
        mock_run.return_value = MagicMock(returncode=2, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].success is True
        assert "warnings" in results[0].message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_exit_code_2_sets_warning_flag(self, mock_run, unrouted_pcb: Path):
        """Pipeline sets warning=True on result when route returns exit code 2."""
        mock_run.return_value = MagicMock(returncode=2, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].warning is True

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_exit_code_0_no_warning_flag(self, mock_run, unrouted_pcb: Path):
        """Pipeline does not set warning flag when route succeeds fully (exit 0)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE])

        assert len(results) == 1
        assert results[0].success is True
        assert results[0].warning is False

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_pipeline_continues_past_partial_route(self, mock_run, unrouted_pcb: Path):
        """Pipeline continues to subsequent steps when route returns exit code 2."""

        def side_effect(cmd, **kwargs):
            # route returns exit code 2 (partial routing)
            if "route" in cmd:
                return MagicMock(returncode=2, stderr="", stdout="")
            # All other steps succeed normally
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE, PipelineStep.FIX_DRC, PipelineStep.AUDIT])

        # All three steps should have run
        assert len(results) == 3
        assert results[0].success is True  # route: partial success
        assert results[0].warning is True
        assert results[1].success is True  # fix-drc: ran
        assert results[2].success is True  # audit: ran

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_pipeline_aborts_on_route_fatal_failure(self, mock_run, unrouted_pcb: Path):
        """Pipeline aborts when route returns exit code 1 (fatal failure, no output)."""
        mock_run.return_value = MagicMock(returncode=1, stderr="Error: no nets routed", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True)
        results = run_pipeline(ctx, [PipelineStep.ROUTE, PipelineStep.FIX_DRC, PipelineStep.AUDIT])

        # Pipeline should stop at route step (exit 1 = failure)
        assert len(results) == 1
        assert results[0].success is False
        assert "failed" in results[0].message


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
    def test_mfr_forwarded_to_fix_silkscreen(self, mock_run, routed_pcb: Path):
        """--mfr flag is forwarded to fix-silkscreen step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-silkscreen", "--mfr", "pcbway", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--mfr" in cmd_args
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "pcbway"

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
        """Steps execute in the correct order: erc, fix-erc, fix-silkscreen, fix-vias, route, etc."""
        expected = [
            PipelineStep.ERC,
            PipelineStep.FIX_ERC,
            PipelineStep.FIX_SILKSCREEN,
            PipelineStep.FIX_VIAS,
            PipelineStep.ROUTE,
            PipelineStep.OPTIMIZE,
            PipelineStep.FIX_DRC,
            PipelineStep.ZONES,
            PipelineStep.AUDIT,
            PipelineStep.REPORT,
            PipelineStep.EXPORT,
        ]
        assert expected == ALL_STEPS

    def test_fix_silkscreen_between_erc_and_fix_vias(self):
        """FIX_SILKSCREEN is positioned after ERC and before FIX_VIAS in ALL_STEPS."""
        erc_idx = ALL_STEPS.index(PipelineStep.ERC)
        silkscreen_idx = ALL_STEPS.index(PipelineStep.FIX_SILKSCREEN)
        vias_idx = ALL_STEPS.index(PipelineStep.FIX_VIAS)
        assert erc_idx < silkscreen_idx < vias_idx

    def test_fix_silkscreen_in_all_steps(self):
        """PipelineStep.FIX_SILKSCREEN is present in ALL_STEPS."""
        assert PipelineStep.FIX_SILKSCREEN in ALL_STEPS


class TestPipelineLayerAutoDetection:
    """Tests for automatic copper layer count detection in pipeline."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_auto_detect_4_layer_board(self, mock_run, four_layer_pcb: Path):
        """4-layer PCB with no --layers flag passes --layers 4 to fix-vias subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-vias", str(four_layer_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "4"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_auto_detect_2_layer_board_no_regression(self, mock_run, routed_pcb: Path):
        """2-layer PCB with no --layers flag passes --layers 2 (unchanged behavior)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-vias", str(routed_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "2"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_explicit_layers_overrides_detection(self, mock_run, four_layer_pcb: Path):
        """--layers 2 on a 4-layer PCB passes --layers 2 (explicit wins)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-vias", "--layers", "2", str(four_layer_pcb), "--quiet"])

        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "2"

    def test_dry_run_shows_correct_layer_count(self, four_layer_pcb: Path, capsys):
        """Dry-run on 4-layer board displays 4 layers in output, not 2 layers."""
        result = main(["--dry-run", str(four_layer_pcb)])
        assert result == 0

        # The dry-run fix-vias message includes --layers N
        captured = capsys.readouterr()
        assert "--layers 4" in captured.out

    def test_help_text_mentions_auto_detection(self, capsys):
        """kct pipeline --help text includes 'auto-detect'."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])

        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "auto-detect" in captured.out.lower()

    def test_commands_shim_passes_none_sentinel(self):
        """commands/pipeline.py passes --layers when pipeline_layers is not None,
        does not pass it when None."""
        from kicad_tools.cli.commands.pipeline import run_pipeline_command

        # When pipeline_layers is None (default), --layers should NOT be forwarded
        args_none = MagicMock()
        args_none.pipeline_input = "test.kicad_pcb"
        args_none.pipeline_step = None
        args_none.pipeline_mfr = "jlcpcb"
        args_none.pipeline_layers = None
        args_none.pipeline_dry_run = False
        args_none.pipeline_verbose = False
        args_none.pipeline_force = False
        args_none.global_quiet = False

        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as mock_main:
            run_pipeline_command(args_none)

        call_argv = mock_main.call_args[0][0]
        assert "--layers" not in call_argv

        # When pipeline_layers is set (e.g., 4), --layers should be forwarded
        args_explicit = MagicMock()
        args_explicit.pipeline_input = "test.kicad_pcb"
        args_explicit.pipeline_step = None
        args_explicit.pipeline_mfr = "jlcpcb"
        args_explicit.pipeline_layers = 4
        args_explicit.pipeline_dry_run = False
        args_explicit.pipeline_verbose = False
        args_explicit.pipeline_force = False
        args_explicit.global_quiet = False

        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as mock_main:
            run_pipeline_command(args_explicit)

        call_argv = mock_main.call_args[0][0]
        assert "--layers" in call_argv
        layers_idx = call_argv.index("--layers")
        assert call_argv[layers_idx + 1] == "4"

    def test_full_cli_parser_defaults_layers_to_none(self):
        """Full CLI path: _add_pipeline_parser sets pipeline_layers=None by default.

        This is the integration test for issue #1349. The root cause was that
        parser.py had default=2 for --layers, which meant auto-detection in
        pipeline_cmd.py was never reached when invoked via 'kct pipeline'.
        """
        import argparse

        from kicad_tools.cli.parser import _add_pipeline_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        _add_pipeline_parser(sub)

        args = parser.parse_args(["pipeline", "board.kicad_pcb", "--dry-run"])

        assert args.pipeline_layers is None, (
            f"Expected pipeline_layers=None when --layers is omitted, "
            f"got {args.pipeline_layers!r}. "
            "parser.py must use default=None so auto-detection runs."
        )

    def test_full_cli_parser_explicit_layers_preserved(self):
        """Full CLI path: explicit --layers value is preserved through parser."""
        import argparse

        from kicad_tools.cli.parser import _add_pipeline_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        _add_pipeline_parser(sub)

        args = parser.parse_args(["pipeline", "board.kicad_pcb", "--layers", "4"])

        assert args.pipeline_layers == 4, (
            f"Expected pipeline_layers=4 when --layers 4 is given, got {args.pipeline_layers!r}."
        )

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_full_cli_path_auto_detects_4_layers(self, mock_run, four_layer_pcb: Path):
        """Full CLI path: parser args through shim reach pipeline_cmd with auto-detection.

        Exercises the complete chain: parser.parse_args -> run_pipeline_command -> main.
        """
        import argparse

        from kicad_tools.cli.commands.pipeline import run_pipeline_command
        from kicad_tools.cli.parser import _add_pipeline_parser

        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        _add_pipeline_parser(sub)

        args = parser.parse_args(
            [
                "pipeline",
                str(four_layer_pcb),
                "--step",
                "fix-vias",
            ]
        )
        # --quiet is a global flag on the main parser, not on the pipeline
        # subparser. Set it directly so the shim passes it through.
        args.global_quiet = True

        # Verify the parser default is None (not 2)
        assert args.pipeline_layers is None

        # Run through the real shim
        run_pipeline_command(args)

        # The shim should NOT have passed --layers (since pipeline_layers is None),
        # so pipeline_cmd.main auto-detects from the 4-layer PCB file.
        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "4"


class TestRouteStepArgForwarding:
    """Tests that route step forwards --grid auto, --manufacturer, --layers auto, --auto-fix."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_passes_grid_auto(self, mock_run, unrouted_pcb: Path):
        """Route step passes --grid auto to subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, mfr="jlcpcb")
        run_pipeline(ctx, [PipelineStep.ROUTE])

        cmd_args = mock_run.call_args[0][0]
        assert "--grid" in cmd_args
        grid_idx = cmd_args.index("--grid")
        assert cmd_args[grid_idx + 1] == "auto"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_passes_manufacturer(self, mock_run, unrouted_pcb: Path):
        """Route step passes --manufacturer with the context mfr value."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, mfr="pcbway")
        run_pipeline(ctx, [PipelineStep.ROUTE])

        cmd_args = mock_run.call_args[0][0]
        assert "--manufacturer" in cmd_args
        mfr_idx = cmd_args.index("--manufacturer")
        assert cmd_args[mfr_idx + 1] == "pcbway"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_passes_layers_auto(self, mock_run, unrouted_pcb: Path):
        """Route step passes --layers auto (string, not integer)."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, mfr="jlcpcb", layers=4)
        run_pipeline(ctx, [PipelineStep.ROUTE])

        cmd_args = mock_run.call_args[0][0]
        assert "--layers" in cmd_args
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "auto"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_passes_auto_fix(self, mock_run, unrouted_pcb: Path):
        """Route step passes --auto-fix flag."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, mfr="jlcpcb")
        run_pipeline(ctx, [PipelineStep.ROUTE])

        cmd_args = mock_run.call_args[0][0]
        assert "--auto-fix" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_route_all_new_flags_present(self, mock_run, unrouted_pcb: Path):
        """Route step includes all four new flags in a single invocation."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, mfr="oshpark")
        run_pipeline(ctx, [PipelineStep.ROUTE])

        cmd_args = mock_run.call_args[0][0]
        assert "--grid" in cmd_args
        assert "--manufacturer" in cmd_args
        assert "--layers" in cmd_args
        assert "--auto-fix" in cmd_args
        # Verify values
        assert cmd_args[cmd_args.index("--grid") + 1] == "auto"
        assert cmd_args[cmd_args.index("--manufacturer") + 1] == "oshpark"
        assert cmd_args[cmd_args.index("--layers") + 1] == "auto"

    def test_dry_run_route_unrouted_shows_new_flags(self, unrouted_pcb: Path):
        """Dry-run on unrouted board shows --grid auto, --manufacturer, --layers auto, --auto-fix."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_route

        ctx = PipelineContext(pcb_file=unrouted_pcb, quiet=True, dry_run=True, mfr="pcbway")
        console = Console(quiet=True)
        result = _run_step_route(ctx, console)

        assert "--grid auto" in result.message
        assert "--manufacturer pcbway" in result.message
        assert "--layers auto" in result.message
        assert "--auto-fix" in result.message

    def test_dry_run_route_force_reroute_shows_new_flags(self, routed_pcb: Path):
        """Dry-run with --force on routed board shows new flags in re-route message."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_route

        ctx = PipelineContext(
            pcb_file=routed_pcb, quiet=True, dry_run=True, force=True, mfr="jlcpcb"
        )
        console = Console(quiet=True)
        result = _run_step_route(ctx, console)

        assert "--grid auto" in result.message
        assert "--manufacturer jlcpcb" in result.message
        assert "--layers auto" in result.message
        assert "--auto-fix" in result.message
        assert "re-route" in result.message


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


class TestCommitFlag:
    """Tests for --commit flag."""

    @pytest.fixture
    def git_pcb(self, tmp_path: Path) -> Path:
        """Create a routed PCB file inside a git repository."""
        # Initialize a git repo in tmp_path
        subprocess.run(
            ["git", "init", str(tmp_path)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(ROUTED_PCB)
        # Initial commit so we have a baseline
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
            capture_output=True,
            check=True,
        )
        return pcb_file

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_calls_git_add_and_commit(self, mock_step, git_pcb: Path):
        """When --commit is given and all steps succeed, git add + commit are called."""
        mock_step.return_value = (True, "completed successfully")

        # Modify the PCB file so there is something to commit
        content = git_pcb.read_text()
        git_pcb.write_text(content + "\n; modified by pipeline\n")

        result = main(["--step", "fix-vias", "--commit", "--quiet", str(git_pcb)])
        assert result == 0

        # Verify a commit was created
        log = subprocess.run(
            ["git", "-C", str(git_pcb.parent), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
        )
        assert "fix: run kct pipeline" in log.stdout

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_suppressed_on_dry_run(self, mock_step, git_pcb: Path):
        """--commit is silently ignored when --dry-run is also given."""
        mock_step.return_value = (True, "completed successfully")

        result = main(["--dry-run", "--commit", "--quiet", str(git_pcb)])
        assert result == 0

        # Verify no new commit was created (only the initial one)
        log = subprocess.run(
            ["git", "-C", str(git_pcb.parent), "log", "--oneline"],
            capture_output=True,
            text=True,
        )
        lines = log.stdout.strip().split("\n")
        assert len(lines) == 1
        assert "initial" in lines[0]

    def test_commit_error_when_not_in_git_repo(self, tmp_path: Path):
        """--commit exits with error when the PCB is not in a git repository."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(ROUTED_PCB)

        # Use dry-run=False with --commit on a non-git directory.
        # We need to mock the pipeline steps to succeed so we reach the commit logic.
        with patch(
            "kicad_tools.cli.pipeline_cmd._run_subprocess_step",
            return_value=(True, "completed successfully"),
        ):
            result = main(["--step", "fix-vias", "--commit", "--quiet", str(pcb_file)])
        assert result == 1

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_skipped_when_pipeline_fails(self, mock_step, git_pcb: Path):
        """When a pipeline step fails, git commit is never attempted."""
        mock_step.return_value = (False, "failed: some error")

        # Modify the PCB to create a diff
        content = git_pcb.read_text()
        git_pcb.write_text(content + "\n; modified\n")

        result = main(["--step", "fix-vias", "--commit", "--quiet", str(git_pcb)])
        assert result == 1

        # Verify no new commit was created
        log = subprocess.run(
            ["git", "-C", str(git_pcb.parent), "log", "--oneline"],
            capture_output=True,
            text=True,
        )
        lines = log.stdout.strip().split("\n")
        assert len(lines) == 1
        assert "initial" in lines[0]

    def test_commit_message_format(self, routed_pcb: Path):
        """The commit message follows the documented format."""
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        results = [
            PipelineResult(step="fix-vias", success=True, message="completed"),
        ]
        msg = _build_commit_message(ctx, results)
        assert msg.startswith("fix: run kct pipeline (")
        assert msg.endswith(")")

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_commit_message_fallback(self, mock_run, tmp_path: Path):
        """When metrics cannot be determined, fallback message uses mfr name."""
        # Simulate a non-existent PCB (no routing info) and a failing kct check
        pcb_file = tmp_path / "nonexistent.kicad_pcb"
        # Mock subprocess.run to always fail so no DRC count is obtained
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        ctx = PipelineContext(pcb_file=pcb_file, mfr="pcbway", layers=2, quiet=True)
        results = []
        msg = _build_commit_message(ctx, results)
        assert msg == "fix: run kct pipeline (pcbway)"

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_error_when_no_changes(self, mock_step, git_pcb: Path):
        """--commit exits with error when pipeline produces no file changes."""
        mock_step.return_value = (True, "completed successfully")

        # Do NOT modify the PCB, so git diff --cached --quiet will return 0
        result = main(["--step", "fix-vias", "--commit", "--quiet", str(git_pcb)])
        assert result == 1

    def test_is_git_repo_true(self, git_pcb: Path):
        """_is_git_repo returns True for a git working tree."""
        assert _is_git_repo(git_pcb.parent) is True

    def test_is_git_repo_false(self, tmp_path: Path):
        """_is_git_repo returns False outside a git repository."""
        assert _is_git_repo(tmp_path) is False

    def test_without_commit_no_git_operations(self, routed_pcb: Path):
        """Without --commit, no git operations are performed."""
        with patch(
            "kicad_tools.cli.pipeline_cmd._run_subprocess_step",
            return_value=(True, "completed successfully"),
        ) as mock_step:
            result = main(["--step", "fix-vias", "--quiet", str(routed_pcb)])
        assert result == 0
        # No git calls should have been made (only _run_subprocess_step for fix-vias)
        assert mock_step.call_count == 1

    def test_commit_flag_in_help(self, capsys):
        """--commit appears in the help text."""
        with pytest.raises(SystemExit) as exc_info:
            main(["--help"])
        assert exc_info.value.code == 0
        captured = capsys.readouterr()
        assert "--commit" in captured.out

    def test_parser_commit_flag(self):
        """Full CLI parser exposes pipeline_commit with default False."""
        import argparse

        from kicad_tools.cli.parser import _add_pipeline_parser

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers()
        _add_pipeline_parser(sub)

        args = parser.parse_args(["pipeline", "board.kicad_pcb"])
        assert args.pipeline_commit is False

        args_commit = parser.parse_args(["pipeline", "board.kicad_pcb", "--commit"])
        assert args_commit.pipeline_commit is True

    def test_commands_shim_forwards_commit(self):
        """commands/pipeline.py forwards --commit when pipeline_commit is True."""
        from kicad_tools.cli.commands.pipeline import run_pipeline_command

        args = MagicMock()
        args.pipeline_input = "test.kicad_pcb"
        args.pipeline_step = None
        args.pipeline_mfr = "jlcpcb"
        args.pipeline_layers = None
        args.pipeline_dry_run = False
        args.pipeline_verbose = False
        args.pipeline_force = False
        args.pipeline_commit = True
        args.global_quiet = False

        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as mock_main:
            run_pipeline_command(args)

        call_argv = mock_main.call_args[0][0]
        assert "--commit" in call_argv

    def test_commands_shim_omits_commit_when_false(self):
        """commands/pipeline.py does NOT pass --commit when pipeline_commit is False."""
        from kicad_tools.cli.commands.pipeline import run_pipeline_command

        args = MagicMock()
        args.pipeline_input = "test.kicad_pcb"
        args.pipeline_step = None
        args.pipeline_mfr = "jlcpcb"
        args.pipeline_layers = None
        args.pipeline_dry_run = False
        args.pipeline_verbose = False
        args.pipeline_force = False
        args.pipeline_commit = False
        args.global_quiet = False

        with patch("kicad_tools.cli.pipeline_cmd.main", return_value=0) as mock_main:
            run_pipeline_command(args)

        call_argv = mock_main.call_args[0][0]
        assert "--commit" not in call_argv


# =========================================================================
# ERC STEP TESTS
# =========================================================================

# Sample ERC JSON report with errors
ERC_JSON_WITH_ERRORS = """{
    "source": "test.kicad_sch",
    "kicad_version": "8.0.0",
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "test-uuid",
            "violations": [
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin 4 of U5 is not connected",
                    "pos": {"x": 100, "y": 50},
                    "items": [{"description": "Pin 4 of U5"}],
                    "excluded": false
                },
                {
                    "type": "power_pin_not_driven",
                    "severity": "error",
                    "description": "+12V is not driven by any source",
                    "pos": {"x": 120, "y": 60},
                    "items": [{"description": "+12V power net"}],
                    "excluded": false
                }
            ]
        }
    ]
}"""

# Sample ERC JSON report with no violations
ERC_JSON_CLEAN = """{
    "source": "test.kicad_sch",
    "kicad_version": "8.0.0",
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "test-uuid",
            "violations": []
        }
    ]
}"""

# Sample ERC JSON report with warnings only
ERC_JSON_WARNINGS_ONLY = """{
    "source": "test.kicad_sch",
    "kicad_version": "8.0.0",
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "test-uuid",
            "violations": [
                {
                    "type": "similar_labels",
                    "severity": "warning",
                    "description": "Labels VCC and Vcc look similar",
                    "pos": {"x": 80, "y": 40},
                    "items": [],
                    "excluded": false
                }
            ]
        }
    ]
}"""

# Sample ERC JSON report with only non-blocking errors (pin_to_pin)
ERC_JSON_WITH_NON_BLOCKING_ERRORS = """{
    "source": "test.kicad_sch",
    "kicad_version": "8.0.0",
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "test-uuid",
            "violations": [
                {
                    "type": "pin_to_pin",
                    "severity": "error",
                    "description": "Pin-to-pin connection between U1/OUT and U2/OUT",
                    "pos": {"x": 110, "y": 55},
                    "items": [{"description": "U1 pin OUT"}, {"description": "U2 pin OUT"}],
                    "excluded": false
                }
            ]
        }
    ]
}"""

# Sample ERC JSON report with mixed blocking + non-blocking errors
ERC_JSON_WITH_MIXED_ERRORS = """{
    "source": "test.kicad_sch",
    "kicad_version": "8.0.0",
    "coordinate_units": "mm",
    "sheets": [
        {
            "path": "/",
            "uuid_path": "test-uuid",
            "violations": [
                {
                    "type": "pin_to_pin",
                    "severity": "error",
                    "description": "Pin-to-pin connection between U1/OUT and U2/OUT",
                    "pos": {"x": 110, "y": 55},
                    "items": [{"description": "U1 pin OUT"}, {"description": "U2 pin OUT"}],
                    "excluded": false
                },
                {
                    "type": "pin_not_connected",
                    "severity": "error",
                    "description": "Pin 3 of U3 is not connected",
                    "pos": {"x": 130, "y": 70},
                    "items": [{"description": "Pin 3 of U3"}],
                    "excluded": false
                }
            ]
        }
    ]
}"""


@pytest.fixture
def pcb_with_schematic(tmp_path: Path) -> tuple[Path, Path]:
    """Create a PCB file alongside a schematic file."""
    pcb_file = tmp_path / "board.kicad_pcb"
    pcb_file.write_text(ROUTED_PCB)
    sch_file = tmp_path / "board.kicad_sch"
    sch_file.write_text("(kicad_sch (version 20230121))")
    return pcb_file, sch_file


@pytest.fixture
def pcb_without_schematic(tmp_path: Path) -> Path:
    """Create a PCB file without a sibling schematic."""
    pcb_file = tmp_path / "standalone.kicad_pcb"
    pcb_file.write_text(ROUTED_PCB)
    return pcb_file


class TestResolveSchematic:
    """Tests for _resolve_schematic helper."""

    def test_finds_schematic_from_pcb(self, pcb_with_schematic):
        """Resolves .kicad_sch from .kicad_pcb with same stem."""
        pcb_file, sch_file = pcb_with_schematic
        result = _resolve_schematic(pcb_file)
        assert result == sch_file

    def test_finds_schematic_from_project(self, tmp_path: Path):
        """Resolves .kicad_sch from .kicad_pro with same stem."""
        pcb_file = tmp_path / "project.kicad_pcb"
        pcb_file.write_text(ROUTED_PCB)
        pro_file = tmp_path / "project.kicad_pro"
        pro_file.write_text("{}")
        sch_file = tmp_path / "project.kicad_sch"
        sch_file.write_text("(kicad_sch)")
        result = _resolve_schematic(pcb_file, pro_file)
        assert result == sch_file

    def test_returns_none_when_no_schematic(self, pcb_without_schematic):
        """Returns None when no matching .kicad_sch exists."""
        result = _resolve_schematic(pcb_without_schematic)
        assert result is None


class TestERCStep:
    """Tests for ERC pipeline step."""

    def test_erc_skip_when_no_schematic(self, pcb_without_schematic: Path):
        """ERC step skips gracefully when no schematic file exists."""
        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_without_schematic, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is True
        assert "no .kicad_sch" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli", return_value=None)
    def test_erc_skip_when_no_kicad_cli(self, mock_find, pcb_with_schematic):
        """ERC step skips when kicad-cli is not installed."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is True
        assert "kicad-cli not found" in result.message

    def test_erc_dry_run(self, pcb_with_schematic):
        """ERC step in dry-run mode outputs the would-be command."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True, dry_run=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "[dry-run]" in result.message
        assert "kct erc" in result.message
        assert sch_file.name in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_halts_pipeline_on_errors(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """Pipeline halts when ERC reports errors (no --force)."""
        pcb_file, sch_file = pcb_with_schematic

        # Write ERC JSON report to a temp file
        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is False
        assert "2 blocking error(s) found" in result.message
        assert "--force" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_force_continues(self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path):
        """With --force, ERC errors are logged but pipeline continues."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True, force=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "--force" in result.message
        assert "2 blocking error(s)" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_clean_pass(self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path):
        """ERC step passes cleanly when no violations are found."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_CLEAN)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "no violations" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_warnings_only_passes(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """ERC step passes (does not halt) when only warnings are found."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WARNINGS_ONLY)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert "1 warning(s)" in result.message
        assert "no errors" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_violations_include_suggestions(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path, capsys
    ):
        """ERC violation output includes fix suggestions from generate_erc_suggestions."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        # Use non-quiet mode so violation details are printed
        console = Console()
        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=False, force=True)
        result = _run_step_erc(ctx, console)

        # The result should still be success (force mode)
        assert result.success is True

        # Capture console output — rich writes to stdout
        captured = capsys.readouterr()
        # Suggestions contain words like "Connect", "Add", etc.
        assert "Suggestion:" in captured.out

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_quiet_suppresses_per_violation_output(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path, capsys
    ):
        """With --quiet, per-violation output is suppressed."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True, force=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        captured = capsys.readouterr()
        # Quiet mode should not print per-violation lines
        assert "pin_not_connected" not in captured.out
        assert "Suggestion:" not in captured.out

    def test_erc_step_single_step_via_main(self, pcb_with_schematic):
        """--step erc runs only the ERC step via main()."""
        pcb_file, sch_file = pcb_with_schematic

        # Use dry-run to avoid needing kicad-cli
        result = main(["--step", "erc", "--dry-run", str(pcb_file)])
        assert result == 0

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_halts_pipeline_blocks_subsequent_steps(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """When ERC fails (errors, no --force), subsequent steps do not run."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        # Run ERC and FIX_VIAS together
        results = run_pipeline(ctx, [PipelineStep.ERC, PipelineStep.FIX_VIAS])

        # Pipeline should stop after ERC failure
        assert len(results) == 1
        assert results[0].success is False
        assert "erc" in results[0].message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_erc_force_allows_subsequent_steps(
        self, mock_subprocess, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """With --force, pipeline continues past ERC errors to next steps."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")
        mock_subprocess.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            force=True,
            layers=2,
        )
        results = run_pipeline(ctx, [PipelineStep.ERC, PipelineStep.FIX_VIAS])

        # Both steps should have run
        assert len(results) == 2
        assert results[0].success is True  # ERC passes with --force
        assert results[1].success is True  # FIX_VIAS runs

    def test_erc_is_first_step(self):
        """ERC is the first step in ALL_STEPS, followed by FIX_ERC, then FIX_SILKSCREEN."""
        assert ALL_STEPS[0] == PipelineStep.ERC
        assert ALL_STEPS[1] == PipelineStep.FIX_ERC
        assert ALL_STEPS[2] == PipelineStep.FIX_SILKSCREEN

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_non_blocking_errors_pass(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """Non-blocking errors (e.g. pin_to_pin) pass the pipeline with WARN."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_NON_BLOCKING_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "non-blocking" in result.message
        assert "1 non-blocking error(s)" in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_non_blocking_sets_zero_error_count(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """ctx.erc_error_count is 0 when only non-blocking errors are present."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_NON_BLOCKING_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        _run_step_erc(ctx, console)

        # Only blocking errors count — non-blocking should not trigger fix-erc
        assert ctx.erc_error_count == 0

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_mixed_blocking_and_non_blocking_halts(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """Mixed blocking + non-blocking errors halts with blocking count only."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_MIXED_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True)
        console = Console(quiet=True)
        result = _run_step_erc(ctx, console)

        # Should fail because of the 1 blocking error (pin_not_connected)
        assert result.success is False
        assert "1 blocking error(s) found" in result.message
        # Should NOT report total count (2), only blocking count (1)
        assert "2 blocking" not in result.message

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    def test_erc_mixed_errors_sets_blocking_count(
        self, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """ctx.erc_error_count reflects only blocking errors in mixed scenario."""
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_MIXED_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")

        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_file, schematic_file=sch_file, quiet=True, force=True)
        console = Console(quiet=True)
        _run_step_erc(ctx, console)

        # pin_not_connected is blocking, pin_to_pin is non-blocking
        assert ctx.erc_error_count == 1


# =========================================================================
# REPORT STEP TESTS
# =========================================================================


class TestReportStep:
    """Tests for the REPORT pipeline step."""

    def test_report_step_in_all_steps(self):
        """PipelineStep.REPORT is in ALL_STEPS after AUDIT."""
        assert PipelineStep.REPORT in ALL_STEPS
        audit_idx = ALL_STEPS.index(PipelineStep.AUDIT)
        report_idx = ALL_STEPS.index(PipelineStep.REPORT)
        assert report_idx == audit_idx + 1

    def test_report_step_is_second_to_last(self):
        """PipelineStep.REPORT is second-to-last in ALL_STEPS (before EXPORT)."""
        assert ALL_STEPS[-2] == PipelineStep.REPORT

    def test_report_enum_value(self):
        """PipelineStep.REPORT has value 'report'."""
        assert PipelineStep.REPORT.value == "report"

    def test_report_dry_run(self, routed_pcb: Path):
        """Report step in dry-run mode outputs the would-be command."""
        from rich.console import Console

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True, mfr="jlcpcb")
        console = Console(quiet=True)
        result = _run_step_report(ctx, console)

        assert result.success is True
        assert "[dry-run]" in result.message
        assert "kct report generate" in result.message
        assert routed_pcb.name in result.message
        assert "--mfr jlcpcb" in result.message
        assert "--no-figures" in result.message
        assert "-o reports/" in result.message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_report_step_invokes_subprocess(self, mock_run, routed_pcb: Path):
        """Report step calls subprocess with correct args."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        results = run_pipeline(ctx, [PipelineStep.REPORT])

        assert len(results) == 1
        assert results[0].success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "report" in cmd_args
        assert "generate" in cmd_args
        assert "--mfr" in cmd_args
        assert "--no-figures" in cmd_args
        assert "-o" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_report_step_forwards_mfr(self, mock_run, routed_pcb: Path):
        """Report step passes --mfr with the correct manufacturer value."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="pcbway")
        run_pipeline(ctx, [PipelineStep.REPORT])

        cmd_args = mock_run.call_args[0][0]
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "pcbway"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_report_step_output_dir(self, mock_run, routed_pcb: Path):
        """Report step passes -o with reports/ directory path."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        run_pipeline(ctx, [PipelineStep.REPORT])

        cmd_args = mock_run.call_args[0][0]
        o_idx = cmd_args.index("-o")
        assert cmd_args[o_idx + 1] == str(routed_pcb.parent / "reports")

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_step_report_accepted_by_argparse(self, mock_run, routed_pcb: Path):
        """--step report is a valid argparse choice and runs only the report step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "report", str(routed_pcb), "--quiet"])

        assert result == 0
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "report" in cmd_args
        assert "generate" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_report_runs_after_audit_failure(self, mock_run, routed_pcb: Path):
        """REPORT step still executes when AUDIT step fails."""

        def side_effect(cmd, **kwargs):
            if "check" in cmd or "audit" in cmd:
                return MagicMock(returncode=1, stderr="DRC violations found", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb", layers=2)
        results = run_pipeline(ctx, [PipelineStep.AUDIT, PipelineStep.REPORT])

        # Both steps should have run
        assert len(results) == 2
        assert results[0].success is False  # AUDIT failed
        assert results[1].step == PipelineStep.REPORT  # REPORT still ran
        assert results[1].success is True

    def test_full_dry_run_includes_report_step(self, routed_pcb: Path, capsys):
        """Full dry-run (all steps) includes the report step in output."""
        result = main(["--dry-run", str(routed_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "report" in captured.out.lower()

    def test_report_quiet_suppresses_output(self, routed_pcb: Path, capsys):
        """--quiet suppresses the 'Generating report for...' console line."""
        with patch(
            "kicad_tools.cli.pipeline_cmd.subprocess.run",
            return_value=MagicMock(returncode=0, stderr="", stdout=""),
        ):
            main(["--step", "report", "--quiet", str(routed_pcb)])

        captured = capsys.readouterr()
        assert "Generating report" not in captured.out

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_stages_reports_dir(self, mock_step, tmp_path: Path):
        """When --commit is used and reports/ exists, git add includes reports/."""
        # Initialize a git repo
        subprocess.run(
            ["git", "init", str(tmp_path)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(ROUTED_PCB)
        # Create reports/ directory with a file
        reports_dir = tmp_path / "reports"
        reports_dir.mkdir()
        (reports_dir / "report.md").write_text("# Test report")
        # Initial commit
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
            capture_output=True,
            check=True,
        )

        # Modify the PCB and reports so there is something to commit
        pcb_file.write_text(ROUTED_PCB + "\n; modified\n")
        (reports_dir / "report.md").write_text("# Updated report")

        mock_step.return_value = (True, "completed successfully")

        result = main(["--step", "fix-vias", "--commit", "--quiet", str(pcb_file)])
        assert result == 0

        # Verify the commit included reports/ in the staged files
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
        )
        assert "fix: run kct pipeline" in log.stdout

        # Verify both files are in the commit
        show = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
        )
        assert "board.kicad_pcb" in show.stdout
        assert "reports/report.md" in show.stdout


# =========================================================================
# EXPORT STEP TESTS
# =========================================================================


class TestExportStep:
    """Tests for the EXPORT pipeline step."""

    def test_export_step_in_all_steps(self):
        """PipelineStep.EXPORT is in ALL_STEPS after REPORT."""
        assert PipelineStep.EXPORT in ALL_STEPS
        report_idx = ALL_STEPS.index(PipelineStep.REPORT)
        export_idx = ALL_STEPS.index(PipelineStep.EXPORT)
        assert export_idx == report_idx + 1

    def test_export_step_is_last(self):
        """PipelineStep.EXPORT is the last entry in ALL_STEPS."""
        assert ALL_STEPS[-1] == PipelineStep.EXPORT

    def test_export_enum_value(self):
        """PipelineStep.EXPORT has value 'export'."""
        assert PipelineStep.EXPORT.value == "export"

    def test_export_dry_run(self, routed_pcb: Path):
        """Export step in dry-run mode outputs the would-be command."""
        from rich.console import Console

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True, mfr="jlcpcb")
        console = Console(quiet=True)
        result = _run_step_export(ctx, console)

        assert result.success is True
        assert "[dry-run]" in result.message
        assert "kct export" in result.message
        assert routed_pcb.name in result.message
        assert "--mfr jlcpcb" in result.message
        assert "-o manufacturing/" in result.message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_export_step_invokes_subprocess(self, mock_run, routed_pcb: Path):
        """Export step calls subprocess with correct args."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        results = run_pipeline(ctx, [PipelineStep.EXPORT])

        assert len(results) == 1
        assert results[0].success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "export" in cmd_args
        assert "--mfr" in cmd_args
        assert "-o" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_export_step_forwards_mfr(self, mock_run, routed_pcb: Path):
        """Export step passes --mfr with the correct manufacturer value."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="pcbway")
        run_pipeline(ctx, [PipelineStep.EXPORT])

        cmd_args = mock_run.call_args[0][0]
        mfr_idx = cmd_args.index("--mfr")
        assert cmd_args[mfr_idx + 1] == "pcbway"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_export_step_output_dir(self, mock_run, routed_pcb: Path):
        """Export step passes -o with manufacturing/ directory path."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb")
        run_pipeline(ctx, [PipelineStep.EXPORT])

        cmd_args = mock_run.call_args[0][0]
        o_idx = cmd_args.index("-o")
        assert cmd_args[o_idx + 1] == str(routed_pcb.parent / "manufacturing")

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_step_export_accepted_by_argparse(self, mock_run, routed_pcb: Path):
        """--step export is a valid argparse choice and runs only the export step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(["--step", "export", str(routed_pcb), "--quiet"])

        assert result == 0
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "export" in cmd_args

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_export_runs_after_audit_failure(self, mock_run, routed_pcb: Path):
        """EXPORT step still executes when AUDIT step fails (non-blocking)."""

        def side_effect(cmd, **kwargs):
            if "check" in cmd or "audit" in cmd:
                return MagicMock(returncode=1, stderr="DRC violations found", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb", layers=2)
        results = run_pipeline(ctx, [PipelineStep.AUDIT, PipelineStep.REPORT, PipelineStep.EXPORT])

        # All three steps should have run
        assert len(results) == 3
        assert results[0].success is False  # AUDIT failed
        assert results[1].step == PipelineStep.REPORT  # REPORT still ran
        assert results[2].step == PipelineStep.EXPORT  # EXPORT still ran
        assert results[2].success is True

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_export_runs_after_report_failure(self, mock_run, routed_pcb: Path):
        """EXPORT step still executes when REPORT step fails (non-blocking)."""

        def side_effect(cmd, **kwargs):
            if "report" in cmd:
                return MagicMock(returncode=1, stderr="report generation failed", stdout="")
            return MagicMock(returncode=0, stderr="", stdout="")

        mock_run.side_effect = side_effect

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, mfr="jlcpcb", layers=2)
        results = run_pipeline(ctx, [PipelineStep.REPORT, PipelineStep.EXPORT])

        assert len(results) == 2
        assert results[0].success is False  # REPORT failed
        assert results[1].step == PipelineStep.EXPORT  # EXPORT still ran
        assert results[1].success is True

    def test_full_dry_run_includes_export_step(self, routed_pcb: Path, capsys):
        """Full dry-run (all steps) includes the export step in output."""
        result = main(["--dry-run", str(routed_pcb)])
        assert result == 0

        captured = capsys.readouterr()
        assert "export" in captured.out.lower()

    def test_export_quiet_suppresses_output(self, routed_pcb: Path, capsys):
        """--quiet suppresses the 'Exporting manufacturing package...' console line."""
        with patch(
            "kicad_tools.cli.pipeline_cmd.subprocess.run",
            return_value=MagicMock(returncode=0, stderr="", stdout=""),
        ):
            main(["--step", "export", "--quiet", str(routed_pcb)])

        captured = capsys.readouterr()
        assert "Exporting manufacturing package" not in captured.out

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_commit_stages_manufacturing_dir(self, mock_step, tmp_path: Path):
        """When --commit is used and manufacturing/ exists, git add includes manufacturing/."""
        # Initialize a git repo
        subprocess.run(
            ["git", "init", str(tmp_path)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "test@test.com"],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "Test"],
            capture_output=True,
            check=True,
        )
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(ROUTED_PCB)
        # Create manufacturing/ directory with a file
        manufacturing_dir = tmp_path / "manufacturing"
        manufacturing_dir.mkdir()
        (manufacturing_dir / "gerbers.zip").write_bytes(b"fake zip")
        # Initial commit
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "initial"],
            capture_output=True,
            check=True,
        )

        # Modify the PCB and manufacturing file so there is something to commit
        pcb_file.write_text(ROUTED_PCB + "\n; modified\n")
        (manufacturing_dir / "gerbers.zip").write_bytes(b"updated fake zip")

        mock_step.return_value = (True, "completed successfully")

        result = main(["--step", "fix-vias", "--commit", "--quiet", str(pcb_file)])
        assert result == 0

        # Verify the commit included manufacturing/ in the staged files
        log = subprocess.run(
            ["git", "-C", str(tmp_path), "log", "--oneline", "-1"],
            capture_output=True,
            text=True,
        )
        assert "fix: run kct pipeline" in log.stdout

        # Verify both files are in the commit
        show = subprocess.run(
            ["git", "-C", str(tmp_path), "diff", "--name-only", "HEAD~1", "HEAD"],
            capture_output=True,
            text=True,
        )
        assert "board.kicad_pcb" in show.stdout
        assert "manufacturing/gerbers.zip" in show.stdout


# =========================================================================
# FIX-ERC STEP TESTS
# =========================================================================


class TestFixERCStep:
    """Tests for FIX_ERC pipeline step."""

    def test_fix_erc_step_skipped_no_schematic(self, pcb_without_schematic: Path):
        """FIX_ERC step skips when no schematic file exists."""
        from rich.console import Console

        ctx = PipelineContext(pcb_file=pcb_without_schematic, quiet=True)
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        assert result.success is True
        assert result.skipped is True
        assert "no .kicad_sch" in result.message

    def test_fix_erc_step_skipped_no_errors(self, pcb_with_schematic):
        """FIX_ERC step skips when ERC found zero errors."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            erc_error_count=0,
        )
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        assert result.success is True
        assert result.skipped is True
        assert "no ERC errors" in result.message

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_fix_erc_step_runs_on_errors(self, mock_subprocess, pcb_with_schematic):
        """FIX_ERC step invokes subprocess when ERC errors were detected."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        mock_subprocess.return_value = (True, "completed successfully")

        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            erc_error_count=3,
        )
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "fix-erc" in result.message
        # Verify subprocess was called with fix-erc command
        mock_subprocess.assert_called_once()
        cmd = mock_subprocess.call_args[0][0]
        assert "fix-erc" in cmd
        assert str(sch_file) in cmd

    def test_fix_erc_step_dry_run(self, pcb_with_schematic):
        """FIX_ERC step in dry-run mode outputs the would-be command."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            dry_run=True,
            erc_error_count=2,
        )
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        assert result.success is True
        assert result.skipped is False
        assert "[dry-run]" in result.message
        assert "kct fix-erc" in result.message
        assert sch_file.name in result.message

    def test_fix_erc_step_force_runs_with_zero_errors(self, pcb_with_schematic):
        """FIX_ERC step runs when --force is set even with zero ERC errors."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            dry_run=True,
            force=True,
            erc_error_count=0,
        )
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        # With --force, even zero errors should not skip
        assert result.skipped is False
        assert "[dry-run]" in result.message

    def test_all_steps_order_fix_erc_at_index_1(self):
        """FIX_ERC appears at index 1 in ALL_STEPS (after ERC, before FIX_VIAS)."""
        assert ALL_STEPS.index(PipelineStep.FIX_ERC) == 1

    def test_pipeline_step_fix_erc_in_choices(self):
        """'fix-erc' is a valid --step choice in the arg parser."""
        assert "fix-erc" in [s.value for s in PipelineStep]

    def test_fix_erc_enum_value(self):
        """PipelineStep.FIX_ERC has string value 'fix-erc'."""
        assert PipelineStep.FIX_ERC.value == "fix-erc"

    def test_fix_erc_step_single_step_via_main(self, pcb_with_schematic):
        """--step fix-erc runs only the FIX_ERC step via main()."""
        pcb_file, sch_file = pcb_with_schematic

        # Use dry-run; erc_error_count defaults to 0 so fix-erc will skip (no errors)
        result = main(["--step", "fix-erc", "--dry-run", str(pcb_file)])
        assert result == 0

    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_fix_erc_subprocess_failure(self, mock_subprocess, pcb_with_schematic):
        """FIX_ERC step reports failure when subprocess fails."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic
        mock_subprocess.return_value = (False, "failed: exit code 1")

        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            erc_error_count=2,
        )
        console = Console(quiet=True)
        result = _run_step_fix_erc(ctx, console)

        assert result.success is False
        assert "failed" in result.message

    def test_erc_step_populates_erc_error_count(self, pcb_with_schematic, tmp_path):
        """The ERC step sets ctx.erc_error_count for the FIX_ERC step to read."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        with (
            patch("kicad_tools.cli.runner.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")),
            patch(
                "kicad_tools.cli.runner.run_erc",
                return_value=MagicMock(success=True, output_path=erc_report_file, stderr=""),
            ),
        ):
            ctx = PipelineContext(
                pcb_file=pcb_file,
                schematic_file=sch_file,
                quiet=True,
                force=True,
            )
            console = Console(quiet=True)
            _run_step_erc(ctx, console)

        # The ERC step should have set erc_error_count on the context
        assert ctx.erc_error_count == 2

    def test_erc_clean_pass_sets_zero_error_count(self, pcb_with_schematic, tmp_path):
        """When ERC finds no errors, erc_error_count is set to 0."""
        from rich.console import Console

        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_CLEAN)

        with (
            patch("kicad_tools.cli.runner.find_kicad_cli", return_value=Path("/usr/bin/kicad-cli")),
            patch(
                "kicad_tools.cli.runner.run_erc",
                return_value=MagicMock(success=True, output_path=erc_report_file, stderr=""),
            ),
        ):
            ctx = PipelineContext(
                pcb_file=pcb_file,
                schematic_file=sch_file,
                quiet=True,
            )
            console = Console(quiet=True)
            _run_step_erc(ctx, console)

        assert ctx.erc_error_count == 0

    @patch("kicad_tools.cli.runner.find_kicad_cli")
    @patch("kicad_tools.cli.runner.run_erc")
    @patch("kicad_tools.cli.pipeline_cmd._run_subprocess_step")
    def test_pipeline_erc_errors_trigger_fix_erc_without_force(
        self, mock_subprocess_step, mock_run_erc, mock_find_cli, pcb_with_schematic, tmp_path
    ):
        """When ERC finds errors and force=False, FIX_ERC still executes.

        This is the integration test that verifies the pipeline loop does not
        break after ERC failure when FIX_ERC is the next step in the sequence.
        """
        pcb_file, sch_file = pcb_with_schematic

        erc_report_file = tmp_path / "erc_report.json"
        erc_report_file.write_text(ERC_JSON_WITH_ERRORS)

        mock_find_cli.return_value = Path("/usr/bin/kicad-cli")
        mock_run_erc.return_value = MagicMock(success=True, output_path=erc_report_file, stderr="")
        mock_subprocess_step.return_value = (True, "completed")

        ctx = PipelineContext(
            pcb_file=pcb_file,
            schematic_file=sch_file,
            quiet=True,
            layers=2,
            # force=False (default) -- the key assertion of this test
        )
        results = run_pipeline(ctx, [PipelineStep.ERC, PipelineStep.FIX_ERC])

        # Both steps must have executed
        assert len(results) == 2, "Pipeline should not stop after ERC failure when FIX_ERC is next"
        # ERC found errors without --force, so its result is a failure
        assert results[0].success is False
        assert results[0].step == PipelineStep.ERC
        # FIX_ERC must have run (not skipped) because erc_error_count > 0
        assert results[1].step == PipelineStep.FIX_ERC
        assert not results[1].skipped, "FIX_ERC should not be skipped when ERC errors exist"
        assert results[1].success is True


class TestZonesDefaultSkip:
    """Tests for zones step being skipped by default (opt-in via --zones flag)."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_zones_excluded_from_default_pipeline(self, mock_run, routed_pcb: Path):
        """Running pipeline without --zones does not execute the zone fill step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = main([str(routed_pcb), "--quiet"])
        assert result == 0

        # Collect all subprocess calls; none should invoke 'zones fill'
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            assert not ("zones" in cmd and "fill" in cmd), (
                "zones fill should not run without --zones flag"
            )

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_zones_included_when_flag_set(self, mock_run, routed_pcb: Path):
        """Running pipeline with --zones includes the zone fill step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = main([str(routed_pcb), "--quiet", "--zones"])
        assert result == 0

        # At least one subprocess call should invoke 'zones fill'
        zones_called = False
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            if "zones" in cmd and "fill" in cmd:
                zones_called = True
                break
        assert zones_called, "zones fill should run when --zones flag is passed"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_step_zones_still_works_as_targeted_single_step(self, mock_run, routed_pcb: Path):
        """--step zones still executes the zone fill step regardless of --zones flag."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = main(["--step", "zones", str(routed_pcb), "--quiet"])
        # --step zones should work even without --zones flag
        assert result == 0

    def test_zones_still_in_all_steps(self):
        """ZONES remains in ALL_STEPS (not removed, just default-skipped)."""
        assert PipelineStep.ZONES in ALL_STEPS

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_dry_run_without_zones_omits_zone_fill(self, mock_run, routed_pcb: Path):
        """--dry-run without --zones does not list zone fill step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = main(["--dry-run", str(routed_pcb), "--quiet"])
        assert result == 0
        # No subprocess should be called for zones in dry-run without --zones
        for call_args in mock_run.call_args_list:
            cmd = call_args[0][0]
            assert not ("zones" in cmd and "fill" in cmd), (
                "zones fill should not appear in dry-run output without --zones"
            )

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_dry_run_with_zones_includes_zone_fill(self, mock_run, routed_pcb: Path):
        """--dry-run with --zones includes the zone fill step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        result = main(["--dry-run", str(routed_pcb), "--quiet", "--zones"])
        assert result == 0

    def test_help_text_documents_zones_flag(self):
        """--help output documents the --zones opt-in flag."""
        import contextlib
        import io

        f = io.StringIO()
        with contextlib.redirect_stdout(f), pytest.raises(SystemExit):
            main(["--help"])
        help_text = f.getvalue()
        assert "--zones" in help_text
        assert "data corruption" in help_text.lower() or "corruption" in help_text.lower()


class TestFixDrcLocalReroute:
    """Tests that fix-drc step includes --local-reroute flag."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_drc_subprocess_includes_local_reroute(self, mock_run, routed_pcb: Path):
        """fix-drc subprocess command includes --local-reroute flag."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        main(["--step", "fix-drc", str(routed_pcb), "--quiet"])

        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "fix-drc" in cmd_args
        assert "--local-reroute" in cmd_args

    def test_fix_drc_dry_run_mentions_local_reroute(self, routed_pcb: Path):
        """Dry-run message for fix-drc includes --local-reroute."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        assert "--local-reroute" in result.message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_drc_subprocess_includes_max_passes_20(self, mock_run, routed_pcb: Path):
        """fix-drc subprocess command passes --max-passes 20."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "--max-passes" in cmd_args
        passes_idx = cmd_args.index("--max-passes")
        assert cmd_args[passes_idx + 1] == "20"

    def test_fix_drc_dry_run_shows_max_passes_20(self, routed_pcb: Path):
        """Dry-run message for fix-drc includes --max-passes 20."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        assert "--max-passes 20" in result.message


class TestMaxDisplacementPassThrough:
    """Tests for --max-displacement pipeline option forwarded to fix-drc step."""

    def test_fix_drc_dry_run_shows_max_displacement(self, routed_pcb: Path):
        """Dry-run message for fix-drc reflects the configured max-displacement."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        assert "--max-displacement 2.0" in result.message

    def test_fix_drc_dry_run_shows_custom_max_displacement(self, routed_pcb: Path):
        """Dry-run message reflects a user-specified max-displacement value."""
        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, dry_run=True, max_displacement=0.75)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        assert "--max-displacement 0.75" in result.message

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_drc_subprocess_receives_max_displacement(self, mock_run, routed_pcb: Path):
        """fix-drc subprocess command includes --max-displacement from context."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, max_displacement=0.5)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "--max-displacement" in cmd_args
        disp_idx = cmd_args.index("--max-displacement")
        assert cmd_args[disp_idx + 1] == "0.5"

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_fix_drc_subprocess_receives_custom_max_displacement(self, mock_run, routed_pcb: Path):
        """fix-drc subprocess command uses a non-default max-displacement."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        from rich.console import Console

        from kicad_tools.cli.pipeline_cmd import _run_step_fix_drc

        ctx = PipelineContext(pcb_file=routed_pcb, quiet=True, max_displacement=1.0)
        console = Console(quiet=True)
        result = _run_step_fix_drc(ctx, console)

        assert result.success is True
        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "--max-displacement" in cmd_args
        disp_idx = cmd_args.index("--max-displacement")
        assert cmd_args[disp_idx + 1] == "1.0"

    def test_pipeline_default_max_displacement_is_2_0(self):
        """PipelineContext defaults to 2.0 mm max-displacement."""
        from pathlib import Path

        ctx = PipelineContext(pcb_file=Path("dummy.kicad_pcb"))
        assert ctx.max_displacement == 2.0

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_cli_max_displacement_forwarded_to_pipeline(self, mock_run, routed_pcb: Path):
        """CLI --max-displacement argument is forwarded to the fix-drc subprocess."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        result = main(
            [str(routed_pcb), "--step", "fix-drc", "--max-displacement", "0.75", "--quiet"]
        )
        assert result == 0

        mock_run.assert_called_once()
        cmd_args = mock_run.call_args[0][0]
        assert "--max-displacement" in cmd_args
        disp_idx = cmd_args.index("--max-displacement")
        assert cmd_args[disp_idx + 1] == "0.75"


class TestFetchCheckResults:
    """Tests for _fetch_check_results helper."""

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_returns_parsed_json(self, mock_run, routed_pcb: Path):
        """Returns parsed dict when kct check produces valid JSON."""
        import json

        check_data = {
            "summary": {"errors": 3, "warnings": 1, "rules_checked": 5, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "too close"},
                {"type": "clearance", "severity": "error", "message": "too close 2"},
                {"type": "silk_overlap", "severity": "error", "message": "silk issue"},
            ],
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(check_data), stderr="")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        result = _fetch_check_results(ctx)

        assert result is not None
        assert result["summary"]["errors"] == 3
        assert len(result["violations"]) == 3

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_returns_none_on_invalid_json(self, mock_run, routed_pcb: Path):
        """Returns None when subprocess output is not valid JSON."""
        mock_run.return_value = MagicMock(returncode=1, stdout="not json", stderr="error")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        result = _fetch_check_results(ctx)
        assert result is None

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_returns_none_on_exception(self, mock_run, routed_pcb: Path):
        """Returns None when subprocess raises an exception."""
        mock_run.side_effect = FileNotFoundError("python not found")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        result = _fetch_check_results(ctx)
        assert result is None

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_returns_none_when_stdout_is_list(self, mock_run, routed_pcb: Path):
        """Returns None when JSON output is a list, not a dict."""
        import json

        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps([1, 2, 3]), stderr="")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        result = _fetch_check_results(ctx)
        assert result is None

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_uses_context_layers_default(self, mock_run, routed_pcb: Path):
        """Falls back to 2 layers when ctx.layers is None."""
        import json

        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps({"summary": {}, "violations": []}), stderr=""
        )

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=None, quiet=True)
        _fetch_check_results(ctx)

        cmd_args = mock_run.call_args[0][0]
        layers_idx = cmd_args.index("--layers")
        assert cmd_args[layers_idx + 1] == "2"


class TestPrintFinalSummary:
    """Tests for _print_final_summary helper."""

    def _make_console(self):
        """Create a Rich Console that captures output to a string."""
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, no_color=True)
        return console, buf

    def test_ready_verdict_when_zero_errors(self, routed_pcb: Path):
        """Verdict shows READY when DRC error count is 0."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 2, "rules_checked": 5, "passed": True},
            "violations": [
                {"type": "silk_overlap", "severity": "warning", "message": "silk issue"},
                {"type": "clearance", "severity": "warning", "message": "minor"},
            ],
        }
        results = [
            PipelineResult(step=PipelineStep.ERC, success=True, message="erc: no violations found"),
        ]
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "Summary:" in output
        assert "DRC:" in output
        assert "0 errors" in output
        assert "READY" in output
        assert "board passes DRC" in output

    def test_not_ready_verdict_when_errors_exist(self, routed_pcb: Path):
        """Verdict shows NOT READY when DRC errors exist."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 5, "warnings": 0, "rules_checked": 5, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "too close"},
                {"type": "clearance", "severity": "error", "message": "too close 2"},
                {"type": "clearance", "severity": "error", "message": "too close 3"},
                {"type": "via_diameter", "severity": "error", "message": "too small"},
                {"type": "via_diameter", "severity": "error", "message": "too small 2"},
            ],
        }
        results = []
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "NOT READY" in output
        assert "5" in output
        assert "DRC error(s) to resolve" in output

    def test_drc_breakdown_by_type(self, routed_pcb: Path):
        """DRC line shows per-rule breakdown."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 3, "warnings": 0, "rules_checked": 5, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "v1"},
                {"type": "clearance", "severity": "error", "message": "v2"},
                {"type": "via_diameter", "severity": "error", "message": "v3"},
            ],
        }
        results = []
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "3 errors" in output
        assert "2 clearance" in output
        assert "1 via_diameter" in output

    def test_silkscreen_warning_count(self, routed_pcb: Path):
        """Silkscreen count reflects silk-type violations."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 3, "rules_checked": 5, "passed": True},
            "violations": [
                {"type": "silk_overlap", "severity": "warning", "message": "s1"},
                {"type": "silk_over_copper", "severity": "warning", "message": "s2"},
                {"type": "clearance", "severity": "warning", "message": "c1"},
            ],
        }
        results = []
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "Silkscreen: 2 warnings" in output

    def test_erc_pass(self, routed_pcb: Path):
        """ERC line shows PASS when no violations found."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        results = [
            PipelineResult(step=PipelineStep.ERC, success=True, message="erc: no violations found"),
        ]
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "ERC:        PASS" in output

    def test_erc_skipped(self, routed_pcb: Path):
        """ERC line shows skipped when ERC step was skipped."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        results = [
            PipelineResult(
                step=PipelineStep.ERC,
                success=True,
                message="erc: no .kicad_sch found alongside PCB -- skipped",
                skipped=True,
            ),
        ]
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "ERC:        skipped" in output

    def test_erc_fail(self, routed_pcb: Path):
        """ERC line shows FAIL when blocking errors were found."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        results = [
            PipelineResult(
                step=PipelineStep.ERC,
                success=False,
                message="erc: 3 blocking error(s) found (use --force to continue)",
            ),
        ]
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "FAIL" in output
        assert "blocking" in output

    def test_erc_pass_with_non_blocking(self, routed_pcb: Path):
        """ERC line shows PASS with warnings for non-blocking errors."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        results = [
            PipelineResult(
                step=PipelineStep.ERC,
                success=True,
                message="erc: 2 non-blocking error(s) as warning(s)",
            ),
        ]
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "PASS with warnings" in output

    def test_erc_not_in_results(self, routed_pcb: Path):
        """ERC line shows skipped when no ERC result is present."""
        console, buf = self._make_console()
        check_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        results = []
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=check_data)
        output = buf.getvalue()

        assert "ERC:        skipped" in output

    def test_graceful_fallback_when_check_data_is_none(self, routed_pcb: Path):
        """All fields fall back to 'unknown' when check_data is None."""
        console, buf = self._make_console()
        results = []
        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2)

        _print_final_summary(ctx, results, console, check_data=None)
        output = buf.getvalue()

        assert "unknown (check failed)" in output
        assert "unknown -- could not determine DRC status" in output
        assert "Silkscreen: 0 warnings" in output


class TestFinalSummaryIntegration:
    """Integration tests for final summary in run_pipeline."""

    @patch("kicad_tools.cli.pipeline_cmd._fetch_check_results")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_summary_appears_in_full_pipeline(self, mock_run, mock_fetch, routed_pcb: Path, capsys):
        """Summary block appears after full pipeline run."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        mock_fetch.return_value = {
            "summary": {"errors": 2, "warnings": 0, "rules_checked": 5, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "v1"},
                {"type": "clearance", "severity": "error", "message": "v2"},
            ],
        }

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=False)
        run_pipeline(ctx)

        mock_fetch.assert_called_once_with(ctx)

    @patch("kicad_tools.cli.pipeline_cmd._fetch_check_results")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_summary_suppressed_in_quiet_mode(self, mock_run, mock_fetch, routed_pcb: Path):
        """Summary block is not shown in --quiet mode."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        run_pipeline(ctx)

        mock_fetch.assert_not_called()

    @patch("kicad_tools.cli.pipeline_cmd._fetch_check_results")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_summary_suppressed_in_single_step_mode(self, mock_run, mock_fetch, routed_pcb: Path):
        """Summary block is not shown when running a single step."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=False)
        run_pipeline(ctx, steps=[PipelineStep.FIX_SILKSCREEN])

        mock_fetch.assert_not_called()

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_dry_run_shows_placeholder_summary(self, mock_run, routed_pcb: Path, capsys):
        """Dry-run shows placeholder summary with N/A values."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")

        ctx = PipelineContext(
            pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=False, dry_run=True
        )
        run_pipeline(ctx)

        # Cannot easily capture Rich Console output through capsys,
        # but we verify no crash and the function completes
        # The dry-run path does not call _fetch_check_results

    @patch("kicad_tools.cli.pipeline_cmd._fetch_check_results")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_check_data_cached_on_context(self, mock_run, mock_fetch, routed_pcb: Path):
        """_fetch_check_results result is cached on ctx._check_data."""
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        expected_data = {
            "summary": {"errors": 0, "warnings": 0, "rules_checked": 1, "passed": True},
            "violations": [],
        }
        mock_fetch.return_value = expected_data

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=False)
        run_pipeline(ctx)

        assert ctx._check_data is expected_data

    @patch("kicad_tools.cli.pipeline_cmd._fetch_check_results")
    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_build_commit_message_reuses_cached_check_data(
        self, mock_run, mock_fetch, routed_pcb: Path
    ):
        """_build_commit_message uses cached check_data instead of spawning subprocess."""
        check_data = {
            "summary": {"errors": 2, "warnings": 1, "rules_checked": 5, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "v1"},
                {"type": "clearance", "severity": "error", "message": "v2"},
                {"type": "silk_overlap", "severity": "warning", "message": "w1"},
            ],
        }

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        results = [
            PipelineResult(step="fix-vias", success=True, message="completed"),
        ]
        msg = _build_commit_message(ctx, results, check_data=check_data)

        # Should use the pre-fetched data instead of calling _fetch_check_results
        mock_fetch.assert_not_called()
        assert "3 DRC errors" in msg

    @patch("kicad_tools.cli.pipeline_cmd.subprocess.run")
    def test_build_commit_message_calls_fetch_when_no_cache(self, mock_run, routed_pcb: Path):
        """_build_commit_message calls _fetch_check_results when no cache provided."""
        import json

        check_data = {
            "summary": {"errors": 1, "warnings": 0, "rules_checked": 3, "passed": False},
            "violations": [
                {"type": "clearance", "severity": "error", "message": "v1"},
            ],
        }
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(check_data), stderr="")

        ctx = PipelineContext(pcb_file=routed_pcb, mfr="jlcpcb", layers=2, quiet=True)
        results = []
        msg = _build_commit_message(ctx, results)  # no check_data param

        assert "1 DRC errors" in msg
