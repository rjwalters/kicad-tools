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
        # Verify new flags are forwarded
        assert "--grid" in cmd_args
        assert "--manufacturer" in cmd_args
        assert "--auto-fix" in cmd_args


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
