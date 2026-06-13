"""Tests for --auto-fix and --auto-fix-passes flags in route command.

Verifies:
- fix-drc suggestion appears in DRC failure output
- --auto-fix flag triggers fix_drc_cmd.main() after DRC errors
- --auto-fix-passes implies --auto-fix when > 1
- --dry-run suppresses auto-fix
- --skip-drc suppresses auto-fix
- _should_auto_fix helper logic
- _run_auto_fix invokes fix_drc_cmd.main with correct arguments
"""

import contextlib
from io import StringIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kicad_tools.cli.route_cmd import _run_auto_fix, _should_auto_fix


class TestShouldAutoFix:
    """Tests for _should_auto_fix helper function."""

    def test_auto_fix_enabled(self):
        """Returns True when --auto-fix is set."""
        args = SimpleNamespace(auto_fix=True, dry_run=False, skip_drc=False)
        assert _should_auto_fix(args) is True

    def test_auto_fix_disabled(self):
        """Returns False when --auto-fix is not set."""
        args = SimpleNamespace(auto_fix=False, dry_run=False, skip_drc=False)
        assert _should_auto_fix(args) is False

    def test_auto_fix_passes_implies_auto_fix_after_normalization(self):
        """--auto-fix-passes sets auto_fix=True after normalization in main()."""
        # After main() normalization, auto_fix is True when auto_fix_passes is set
        args = SimpleNamespace(auto_fix=True, dry_run=False, skip_drc=False)
        assert _should_auto_fix(args) is True

    def test_auto_fix_false_without_flag(self):
        """auto_fix=False means no auto-fix."""
        args = SimpleNamespace(auto_fix=False, dry_run=False, skip_drc=False)
        assert _should_auto_fix(args) is False

    def test_dry_run_suppresses_auto_fix(self):
        """--dry-run suppresses auto-fix even when --auto-fix is set."""
        args = SimpleNamespace(auto_fix=True, dry_run=True, skip_drc=False)
        assert _should_auto_fix(args) is False

    def test_skip_drc_suppresses_auto_fix(self):
        """--skip-drc suppresses auto-fix even when --auto-fix is set."""
        args = SimpleNamespace(auto_fix=True, dry_run=False, skip_drc=True)
        assert _should_auto_fix(args) is False

    def test_dry_run_and_skip_drc_suppress_auto_fix(self):
        """Both --dry-run and --skip-drc suppress auto-fix."""
        args = SimpleNamespace(auto_fix=True, dry_run=True, skip_drc=True)
        assert _should_auto_fix(args) is False

    def test_missing_attributes_uses_defaults(self):
        """Missing attributes fall back to safe defaults via getattr."""
        args = SimpleNamespace()
        assert _should_auto_fix(args) is False

    def test_auto_fix_with_dry_run_still_suppressed(self):
        """--auto-fix with --dry-run is still suppressed."""
        args = SimpleNamespace(auto_fix=True, dry_run=True, skip_drc=False)
        assert _should_auto_fix(args) is False


class TestRunAutoFix:
    """Tests for _run_auto_fix helper function."""

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_calls_fix_drc(self, mock_fix_drc):
        """_run_auto_fix calls fix_drc_cmd.main with correct arguments."""
        from pathlib import Path

        mock_fix_drc.return_value = 0
        result = _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=True)

        assert result == 0
        mock_fix_drc.assert_called_once()
        call_args = mock_fix_drc.call_args[0][0]
        assert "/tmp/board.kicad_pcb" in call_args
        assert "--max-passes" in call_args
        assert "1" in call_args
        assert "--quiet" in call_args

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_passes_max_passes(self, mock_fix_drc):
        """_run_auto_fix forwards max_passes to fix_drc_cmd."""
        from pathlib import Path

        mock_fix_drc.return_value = 0
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=5, quiet=True)

        call_args = mock_fix_drc.call_args[0][0]
        passes_idx = call_args.index("--max-passes")
        assert call_args[passes_idx + 1] == "5"

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_returns_nonzero_on_failure(self, mock_fix_drc):
        """_run_auto_fix returns non-zero when fix-drc fails."""
        from pathlib import Path

        mock_fix_drc.return_value = 1
        result = _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=True)
        assert result == 1

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_not_quiet(self, mock_fix_drc, capsys):
        """_run_auto_fix prints status when not quiet."""
        from pathlib import Path

        mock_fix_drc.return_value = 0
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        assert "all targeted violations repaired" in captured.out

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_not_quiet_failure(self, mock_fix_drc, capsys):
        """_run_auto_fix prints failure message when not quiet and fix fails.

        Issue #2839: exit code 1 (no progress) now emits a distinct
        ``no progress made`` message instead of the generic
        ``some violations remain`` -- the latter is reserved for the
        catch-all "unknown exit code" branch.
        """
        from pathlib import Path

        mock_fix_drc.return_value = 1
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        assert "no progress made" in captured.out

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_quiet_no_output(self, mock_fix_drc, capsys):
        """_run_auto_fix suppresses output when quiet."""
        from pathlib import Path

        mock_fix_drc.return_value = 0
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=True)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" not in captured.out

    # ── Issue #2839: distinct exit-code messages ─────────────────────

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_auto_fix_rollback_message_distinguishable(self, mock_fix_drc, capsys):
        """Exit code 3 (connectivity rollback) emits a distinct, named message.

        Issue #2839 sub-bug #1: previously, ``_run_auto_fix`` collapsed
        all non-zero exit codes into a single generic ``some violations
        remain`` message.  After the fix, the rollback case (exit 3)
        emits a message containing ``rolled back`` and ``connectivity``
        so the user knows the work was *attempted* and *thrown away*
        (rather than never having happened).
        """
        from pathlib import Path

        mock_fix_drc.return_value = 3  # connectivity rollback
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        # Distinct message keywords -- must mention rollback AND connectivity
        out_lower = captured.out.lower()
        assert "rolled back" in out_lower or "rollback" in out_lower, (
            f"Expected 'rolled back'/'rollback' in output, got: {captured.out!r}"
        )
        assert "connectivity" in out_lower, (
            f"Expected 'connectivity' in output, got: {captured.out!r}"
        )
        # Must NOT collapse into the generic "some violations remain"
        # catch-all (which was the pre-fix behavior).
        assert "some violations remain" not in captured.out, (
            f"Generic catch-all message leaked into rollback path: {captured.out!r}"
        )
        # Must point the user at the documented escape hatch.
        assert "--no-connectivity-check" in captured.out, (
            f"Expected '--no-connectivity-check' guidance in output, got: {captured.out!r}"
        )

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_auto_fix_partial_repair_message(self, mock_fix_drc, capsys):
        """Exit code 2 (partial repair) emits its own distinct message.

        Issue #2839 sub-bug #1: each documented fix-drc exit code gets a
        distinct message so the user can tell ``no progress`` (1) from
        ``partial repair`` (2) from ``connectivity rollback`` (3).
        """
        from pathlib import Path

        mock_fix_drc.return_value = 2  # partial repair
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        # Exit 2 should mention "partial" -- not "rolled back".
        assert "partial" in captured.out.lower()
        assert "rolled back" not in captured.out.lower()
        assert "rollback" not in captured.out.lower()

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_auto_fix_no_progress_message(self, mock_fix_drc, capsys):
        """Exit code 1 (no progress) emits its own distinct message."""
        from pathlib import Path

        mock_fix_drc.return_value = 1  # no progress
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        # Exit 1 should mention "no progress" -- not "rolled back".
        assert "no progress" in captured.out.lower()
        assert "rolled back" not in captured.out.lower()
        assert "rollback" not in captured.out.lower()

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_auto_fix_success_message(self, mock_fix_drc, capsys):
        """Exit code 0 (success) emits the celebratory message (regression guard).

        Issue #2839 synthetic positive control: a non-regressing case
        must still report ``all targeted violations repaired`` so the
        layer-1 visibility fix does not over-correct the happy path.
        """
        from pathlib import Path

        mock_fix_drc.return_value = 0  # success
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "all targeted violations repaired" in captured.out.lower()
        # Success path must not accidentally print rollback wording.
        assert "rolled back" not in captured.out.lower()
        assert "rollback" not in captured.out.lower()
        assert "partial" not in captured.out.lower()


class TestFixDrcSuggestionInDrcOutput:
    """Tests that fix-drc suggestion appears in run_post_route_drc output."""

    @patch("kicad_tools.validate.DRCChecker")
    @patch("kicad_tools.schema.pcb.PCB")
    def test_fix_drc_suggestion_shown_on_errors(
        self, mock_pcb_cls, mock_checker_cls, capsys, tmp_path
    ):
        """run_post_route_drc shows fix-drc suggestion when there are DRC errors."""
        from kicad_tools.cli.route_cmd import run_post_route_drc

        # Create a mock PCB file
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        # Set up mock DRC results with errors
        mock_violation = MagicMock()
        mock_violation.rule_id = "clearance"
        mock_violation.message = "Clearance violation"
        mock_violation.location = (100.0, 100.0)

        mock_results = MagicMock()
        mock_results.error_count = 3
        mock_results.warning_count = 0
        mock_results.errors = [mock_violation]
        mock_results.warnings = []

        mock_checker = MagicMock()
        mock_checker.check_all.return_value = mock_results
        mock_checker_cls.return_value = mock_checker
        mock_pcb_cls.load.return_value = MagicMock()

        run_post_route_drc(
            output_path=pcb_file,
            manufacturer="jlcpcb",
            layers=2,
            quiet=False,
        )

        captured = capsys.readouterr()
        assert "kct fix-drc" in captured.out
        assert "auto-repair clearance violations" in captured.out

    @patch("kicad_tools.validate.DRCChecker")
    @patch("kicad_tools.schema.pcb.PCB")
    def test_fix_drc_suggestion_not_shown_when_no_errors(
        self, mock_pcb_cls, mock_checker_cls, capsys, tmp_path
    ):
        """run_post_route_drc does not show fix-drc suggestion when DRC passes."""
        from kicad_tools.cli.route_cmd import run_post_route_drc

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        mock_results = MagicMock()
        mock_results.error_count = 0
        mock_results.warning_count = 0
        mock_results.errors = []
        mock_results.warnings = []

        mock_checker = MagicMock()
        mock_checker.check_all.return_value = mock_results
        mock_checker_cls.return_value = mock_checker
        mock_pcb_cls.load.return_value = MagicMock()

        run_post_route_drc(
            output_path=pcb_file,
            manufacturer="jlcpcb",
            layers=2,
            quiet=False,
        )

        captured = capsys.readouterr()
        assert "kct fix-drc" not in captured.out

    @patch("kicad_tools.validate.DRCChecker")
    @patch("kicad_tools.schema.pcb.PCB")
    def test_fix_drc_suggestion_not_shown_when_quiet(
        self, mock_pcb_cls, mock_checker_cls, capsys, tmp_path
    ):
        """run_post_route_drc does not show any output when quiet."""
        from kicad_tools.cli.route_cmd import run_post_route_drc

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        mock_results = MagicMock()
        mock_results.error_count = 3
        mock_results.warning_count = 0
        mock_results.errors = []
        mock_results.warnings = []

        mock_checker = MagicMock()
        mock_checker.check_all.return_value = mock_results
        mock_checker_cls.return_value = mock_checker
        mock_pcb_cls.load.return_value = MagicMock()

        run_post_route_drc(
            output_path=pcb_file,
            manufacturer="jlcpcb",
            layers=2,
            quiet=True,
        )

        captured = capsys.readouterr()
        assert captured.out == ""


class TestAutoFixCLIArgs:
    """Tests for --auto-fix and --auto-fix-passes CLI argument parsing."""

    def test_auto_fix_flag_parsed(self):
        """Parser correctly parses --auto-fix flag."""
        from kicad_tools.cli.route_cmd import main

        buf = StringIO()
        with contextlib.redirect_stdout(buf), contextlib.suppress(SystemExit):
            main(["--help"])

        text = buf.getvalue()
        assert "--auto-fix" in text
        assert "--auto-fix-passes" in text

    def test_auto_fix_passes_validation_rejects_zero(self, tmp_path):
        """Parser rejects --auto-fix-passes 0."""
        from kicad_tools.cli.route_cmd import main

        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        result = main(
            [
                str(pcb_file),
                "--auto-fix-passes",
                "0",
                "--dry-run",
                "--quiet",
            ]
        )
        assert result == 1

    def test_auto_fix_passes_normalization(self, tmp_path):
        """--auto-fix-passes implies --auto-fix after normalization."""
        import argparse

        # Parse arguments directly to check normalization
        parser = argparse.ArgumentParser()
        parser.add_argument("pcb")
        parser.add_argument("--auto-fix", action="store_true")
        parser.add_argument("--auto-fix-passes", type=int, default=None)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--skip-drc", action="store_true")

        args = parser.parse_args(["test.kicad_pcb", "--auto-fix-passes", "5"])

        # Before normalization, auto_fix is False
        assert args.auto_fix is False

        # After normalization (as done in main)
        if args.auto_fix_passes is not None:
            args.auto_fix = True
        else:
            args.auto_fix_passes = 3
        assert args.auto_fix is True
        assert args.auto_fix_passes == 5

    def test_auto_fix_passes_default_applied(self, tmp_path):
        """Default auto_fix_passes is 3 when not explicitly provided."""
        import argparse

        parser = argparse.ArgumentParser()
        parser.add_argument("pcb")
        parser.add_argument("--auto-fix", action="store_true")
        parser.add_argument("--auto-fix-passes", type=int, default=None)

        args = parser.parse_args(["test.kicad_pcb", "--auto-fix"])

        # auto_fix_passes is None (not provided), auto_fix is True
        assert args.auto_fix is True
        assert args.auto_fix_passes is None

        # After normalization
        if args.auto_fix_passes is None:
            args.auto_fix_passes = 3
        assert args.auto_fix_passes == 3


class TestFixDrcSuggestionInMainOutput:
    """Tests that fix-drc suggestions appear in main() DRC failure output."""

    def test_suggestions_block_includes_fix_drc(self):
        """The suggestions block in route_cmd source includes fix-drc command."""
        from pathlib import Path

        route_cmd_path = (
            Path(__file__).parent.parent / "src" / "kicad_tools" / "cli" / "route_cmd.py"
        )
        source = route_cmd_path.read_text()

        # Check the suggestions block includes fix-drc
        assert "kct fix-drc" in source
        assert "--auto-fix" in source

    def test_suggestions_block_includes_auto_fix_hint(self):
        """The suggestions block mentions --auto-fix re-route option."""
        from pathlib import Path

        route_cmd_path = (
            Path(__file__).parent.parent / "src" / "kicad_tools" / "cli" / "route_cmd.py"
        )
        source = route_cmd_path.read_text()

        # Check for the auto-fix suggestion in the DRC failure block
        assert "kct route" in source
        assert "--auto-fix" in source


class TestAutoFixViaCentralizedCLI:
    """Tests that --auto-fix, --auto-fix-passes, --skip-drc work via kct route (centralized CLI)."""

    def test_centralized_cli_auto_fix_dry_run(self, tmp_path):
        """kct route ... --auto-fix --dry-run executes without 'unrecognized arguments'."""
        from kicad_tools.cli import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        # Use --grid auto to avoid grid>clearance validation failure
        result = main(
            ["route", str(pcb_file), "--auto-fix", "--dry-run", "--quiet", "--grid", "auto"]
        )
        # Should not fail with unrecognized arguments; exit 0 on dry-run with minimal PCB
        assert result == 0

    def test_centralized_cli_auto_fix_passes_dry_run(self, tmp_path):
        """kct route ... --auto-fix-passes 5 --dry-run executes without error."""
        from kicad_tools.cli import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        result = main(
            [
                "route",
                str(pcb_file),
                "--auto-fix-passes",
                "5",
                "--dry-run",
                "--quiet",
                "--grid",
                "auto",
            ]
        )
        assert result == 0

    def test_centralized_cli_skip_drc_dry_run(self, tmp_path):
        """kct route ... --skip-drc --dry-run executes without error."""
        from kicad_tools.cli import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        result = main(
            ["route", str(pcb_file), "--skip-drc", "--dry-run", "--quiet", "--grid", "auto"]
        )
        assert result == 0

    def test_centralized_cli_auto_fix_passes_zero_rejected(self, tmp_path):
        """kct route ... --auto-fix-passes 0 is rejected by route_cmd validation."""
        from kicad_tools.cli import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        result = main(
            [
                "route",
                str(pcb_file),
                "--auto-fix-passes",
                "0",
                "--dry-run",
                "--quiet",
                "--grid",
                "auto",
            ]
        )
        # route_cmd.main() rejects --auto-fix-passes 0 with exit code 1
        assert result == 1

    def test_centralized_cli_skip_drc_suppresses_auto_fix(self, tmp_path):
        """kct route ... --auto-fix --skip-drc: skip-drc suppresses auto-fix (no crash)."""
        from kicad_tools.cli import main

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb (version 20240101) (generator test))")

        result = main(
            [
                "route",
                str(pcb_file),
                "--auto-fix",
                "--skip-drc",
                "--dry-run",
                "--quiet",
                "--grid",
                "auto",
            ]
        )
        # Both flags should be accepted; --skip-drc suppresses auto-fix behavior
        assert result == 0


# ─────────────────────────────────────────────────────────────────────────────
# Issue #2852: --auto-fix rollback (exit 3) propagation tests.
#
# When fix_drc_cmd.main returns exit 3 (connectivity rollback), the surrounding
# ``kct route`` process must propagate that as exit 3 -- not 0.  Four call
# sites must agree:
#   1. route_with_layer_escalation (route_cmd.py call site ~L2533)
#   2. route_with_rule_relaxation  (route_cmd.py call site ~L3073)
#   3. route_with_combined_escalation (route_cmd.py call site ~L3662)
#   4. main() single-shot flow     (route_cmd.py call site ~L6168)
#
# Tests below force each function to reach its return statement with the
# auto-fix path engaged, then assert exit code 3 on rollback.
# ─────────────────────────────────────────────────────────────────────────────


def _make_routing_args(**overrides):
    """Build a minimal args namespace that drives the auto-fix path.

    Returns args with ``dry_run=False``, ``skip_drc=False``, and
    ``auto_fix=True`` so that ``_should_auto_fix(args)`` returns True
    and the DRC + auto-fix block actually runs.
    """
    defaults = {
        # Routing engine
        "grid": 0.25,
        "trace_width": 0.2,
        "clearance": 0.15,
        "via_drill": 0.3,
        "via_diameter": 0.6,
        "fine_pitch_clearance": None,
        "manufacturer": "jlcpcb",
        "min_trace": None,
        "min_clearance_floor": None,
        "strategy": "negotiated",
        "iterations": 3,
        "timeout": 60,
        "skip_nets": None,
        "edge_clearance": 0.25,
        "force": False,
        "backend": "python",
        "verbose": False,
        "min_completion": 0.95,
        "no_optimize": True,
        "no_early_stop": False,
        "multi_resolution": False,
        "two_phase": False,
        "per_net_timeout": None,
        "two_phase_iterations": None,
        "batch_routing": False,
        "high_performance": False,
        "hierarchical": False,
        "perturbation": True,
        "mc_trials": 10,
        "escape_routing": None,
        "no_escape_routing": False,
        "diagnostics": False,
        "layers": "auto",
        "max_layers": 6,
        "pcb": "test.kicad_pcb",
        "auto_pour": False,
        "format": "text",
        "export_failed_nets": None,
        "strict": False,
        # Critical: trigger the auto-fix branch
        "dry_run": False,
        "skip_drc": False,
        "auto_fix": True,
        "auto_fix_passes": 1,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_success_router(nets_routed: int = 3, nets_to_route: int = 3):
    """Mock router that reports a 100% successful routing run."""
    router = MagicMock()
    router.nets = {i: [f"pad{j}" for j in range(2)] for i in range(1, nets_to_route + 1)}
    router.grid.width = 50.0
    router.grid.height = 40.0
    router.grid.get_total_overflow.return_value = 0
    router.get_statistics.return_value = {
        "nets_routed": nets_routed,
        "segments": 10,
        "vias": 2,
    }
    router.power_stall_abort = False
    router._pour_nets_without_zones = set()
    router.routes = []
    router.rules.via_diameter = 0.6
    router.rules.min_drill_clearance = 0.0
    router.rules.trace_width = 0.2
    router.rules.trace_clearance = 0.15
    router.net_class_map = None
    return router


def _patch_routing_engine_for_success(stack, router):
    """Apply common patches so a routing function reaches the auto-fix path.

    Returns the ExitStack with patches already entered.  Caller should
    use this within ``with`` so cleanup runs on test exit.
    """

    def mock_load(*args, **kwargs):
        return router, {}

    stack.enter_context(patch("kicad_tools.router.load_pcb_for_routing", side_effect=mock_load))
    stack.enter_context(patch("kicad_tools.router.is_cpp_available", return_value=False))
    stack.enter_context(patch("kicad_tools.router.show_routing_summary"))
    stack.enter_context(patch("kicad_tools.cli.route_cmd._write_routed_pcb"))
    stack.enter_context(patch("kicad_tools.cli.route_cmd._fill_zones_after_route"))
    stack.enter_context(
        patch("kicad_tools.cli.route_cmd._auto_skip_pour_nets", return_value=([], []))
    )
    stack.enter_context(
        patch("kicad_tools.cli.route_cmd._resolve_escape_routing_flag", return_value=None)
    )
    stack.enter_context(
        patch("kicad_tools.cli.route_cmd._should_use_escape_routing", return_value=False)
    )
    return stack


class TestAutoFixRollbackPropagation:
    """Issue #2852: ``--auto-fix`` rollback (exit 3) must surface as exit 3.

    Before #2852, three of the four routing flows discarded the return
    value of ``_run_auto_fix``, so a connectivity rollback (fix-drc exit
    3) silently became exit 0 from ``kct route``.  CI / shell scripts
    that inspect ``$?`` could not detect the rollback.

    After #2852, all four flows propagate ``fix_result == 3`` as exit
    code 3 -- the same code the documented exit-code table already
    reserves for "routing met threshold but DRC is dirty."
    """

    def test_rollback_propagates_in_layer_escalation(self, tmp_path):
        """route_with_layer_escalation must return 3 when --auto-fix rolls back."""
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            # DRC reports violations -> auto-fix is invoked.
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            # Auto-fix rolls back (connectivity regression).
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=3))
            result = route_with_layer_escalation(pcb, out, args, quiet=True)

        assert result == 3, (
            f"Expected exit 3 on --auto-fix rollback in route_with_layer_escalation, got {result}"
        )

    def test_rollback_propagates_in_rule_relaxation(self, tmp_path):
        """route_with_rule_relaxation must return 3 when --auto-fix rolls back."""
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_rule_relaxation
        from kicad_tools.router import LayerStack

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        # One trivial "user" tier so the rule-relaxation loop exits after one
        # attempt that succeeds at 100%.
        from dataclasses import dataclass

        @dataclass
        class FakeTier:
            tier: int
            description: str
            trace_width: float
            clearance: float
            via_drill: float = 0.3
            via_diameter: float = 0.6

        tiers = [FakeTier(0, "user", 0.2, 0.15)]

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            stack.enter_context(
                patch("kicad_tools.router.get_relaxation_tiers", return_value=tiers)
            )
            stack.enter_context(
                patch(
                    "kicad_tools.router.io.detect_layer_stack",
                    return_value=LayerStack.two_layer(),
                )
            )
            stack.enter_context(
                patch(
                    "kicad_tools.router.get_mfr_limits",
                    return_value=MagicMock(min_trace=0.127, min_clearance=0.127),
                )
            )
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=3))
            result = route_with_rule_relaxation(pcb, out, args, quiet=True)

        assert result == 3, (
            f"Expected exit 3 on --auto-fix rollback in route_with_rule_relaxation, got {result}"
        )

    def test_rollback_propagates_in_combined_escalation(self, tmp_path):
        """route_with_combined_escalation must return 3 when --auto-fix rolls back."""
        from contextlib import ExitStack
        from dataclasses import dataclass

        from kicad_tools.cli.route_cmd import route_with_combined_escalation

        @dataclass
        class FakeTier:
            tier: int
            description: str
            trace_width: float
            clearance: float
            via_drill: float = 0.3
            via_diameter: float = 0.6

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args(max_layers=2)
        router = _make_success_router(nets_routed=3, nets_to_route=3)
        tiers = [FakeTier(0, "user", 0.2, 0.15)]

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            stack.enter_context(
                patch("kicad_tools.router.get_relaxation_tiers", return_value=tiers)
            )
            stack.enter_context(
                patch(
                    "kicad_tools.router.get_mfr_limits",
                    return_value=MagicMock(min_trace=0.127, min_clearance=0.127),
                )
            )
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=3))
            result = route_with_combined_escalation(pcb, out, args, quiet=True)

        assert result == 3, (
            f"Expected exit 3 on --auto-fix rollback in "
            f"route_with_combined_escalation, got {result}"
        )

    def test_rollback_propagates_in_main_flow(self, tmp_path):
        """main() single-shot flow must return 3 when --auto-fix rolls back.

        The main flow at L6168 captures ``fix_result`` and (per #2852) returns
        exit 3 explicitly when rollback fires, instead of relying on the
        accidentally-correct ``drc_errors`` fall-through path.
        """
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import main as route_main

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")

        with ExitStack() as stack:
            # DRC reports violations.
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            # Auto-fix rolls back.
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=3))
            # Drive main() to the auto-fix branch via the centralized CLI in
            # single-shot mode (no --auto-layers / --auto-rules).  We supply
            # --skip-* knobs that bypass the heavy machinery while still
            # letting DRC + auto-fix fire.
            #
            # The simplest way is to invoke the function via its argv parser
            # and mock the routing engine boundary.  Because the single-shot
            # path's routing-engine setup is not as cleanly mockable as the
            # multi-attempt paths, we instead patch the routing-stage seam.
            router = _make_success_router(nets_routed=3, nets_to_route=3)

            def mock_load(*args, **kwargs):
                return router, {}

            stack.enter_context(
                patch("kicad_tools.router.load_pcb_for_routing", side_effect=mock_load)
            )
            stack.enter_context(patch("kicad_tools.router.is_cpp_available", return_value=False))
            stack.enter_context(patch("kicad_tools.router.show_routing_summary"))
            stack.enter_context(patch("kicad_tools.cli.route_cmd._write_routed_pcb"))
            stack.enter_context(patch("kicad_tools.cli.route_cmd._fill_zones_after_route"))
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd._auto_skip_pour_nets",
                    return_value=([], []),
                )
            )
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd._resolve_escape_routing_flag",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd._should_use_escape_routing",
                    return_value=False,
                )
            )

            result = route_main(
                [
                    str(pcb),
                    "--auto-fix",
                    "--quiet",
                    "--grid",
                    "0.25",
                    "--no-optimize",
                ]
            )

        assert result == 3, f"Expected exit 3 on --auto-fix rollback in main() flow, got {result}"


class TestAutoFixSuccessKeepsZeroExitCode:
    """Issue #2852: regression guard -- happy path (auto-fix exit 0) keeps exit 0.

    The propagation logic must be rollback-only; it must NOT downgrade
    a successful auto-fix run to a non-zero exit.
    """

    def test_layer_escalation_returns_zero_on_auto_fix_success(self, tmp_path):
        """When --auto-fix returns 0, route_with_layer_escalation still returns 0."""
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=0))
            result = route_with_layer_escalation(pcb, out, args, quiet=True)

        assert result == 0, (
            f"Expected exit 0 on --auto-fix success in route_with_layer_escalation, got {result}"
        )


class TestAutoFixNoRegressionForCodes1And2:
    """Issue #2852: fix-drc exit 1 (no progress) and 2 (partial) must NOT override.

    Only exit 3 (connectivity rollback) triggers the new override.  Exit
    codes 1 and 2 must preserve the existing routing-driven exit code --
    i.e. exit 0 for the success-then-DRC-errors-remain path (because the
    routing flows other than main() don't consult ``drc_errors`` at all).

    Note: ``route_with_layer_escalation`` (and siblings) derive their
    exit code purely from ``final_result.success`` -- so a successful
    routing run with fix-drc returning 1 or 2 returns 0.  Only main()
    has separate DRC bookkeeping that surfaces exit 3 from a positive
    ``drc_errors`` count.
    """

    def test_layer_escalation_exit1_preserves_routing_exit_code(self, tmp_path):
        """fix-drc exit 1 (no progress) does not override route exit code."""
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=1))
            result = route_with_layer_escalation(pcb, out, args, quiet=True)

        # success=True path: fix-drc exit 1 must NOT promote to 3.
        assert result == 0, (
            f"fix-drc exit 1 (no progress) must not override route exit "
            f"in route_with_layer_escalation, got {result}"
        )

    def test_layer_escalation_exit2_preserves_routing_exit_code(self, tmp_path):
        """fix-drc exit 2 (partial repair) does not override route exit code."""
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )
            stack.enter_context(patch("kicad_tools.cli.route_cmd._run_auto_fix", return_value=2))
            result = route_with_layer_escalation(pcb, out, args, quiet=True)

        # success=True path: fix-drc exit 2 must NOT promote to 3.
        assert result == 0, (
            f"fix-drc exit 2 (partial) must not override route exit "
            f"in route_with_layer_escalation, got {result}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Issue #3238: exit code 7 for "auto-fix requested but skipped by deadline"
# ─────────────────────────────────────────────────────────────────────────────


class TestAutoFixSkippedExitCode:
    """Issue #3238: when auto-fix is requested but skipped due to
    ``--timeout`` exhaustion, the route command exits with the distinct
    code 7 (not the generic exit 3 "DRC dirty" or exit 0 "success").

    This is the structural guard that prevents the chorus regression
    from hiding again: CI can gate on exit-code 7 OR on the stderr
    token ``AUTOFIX_SKIPPED_BUDGET_EXHAUSTED`` to detect silent
    skip-on-deadline regressions without parsing the full route log.
    """

    def test_layer_escalation_exit7_on_skipped_deadline(self, tmp_path):
        """``route_with_layer_escalation`` returns 7 when
        ``args._auto_fix_status == "skipped_deadline"`` on a successful
        routing run -- the skip overrides the would-be exit 0.
        """
        from contextlib import ExitStack

        from kicad_tools.cli.route_cmd import route_with_layer_escalation

        pcb = tmp_path / "test.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20240101))")
        out = tmp_path / "out.kicad_pcb"

        args = _make_routing_args()
        # Simulate the deadline-skip path having been hit during DRC.
        args._auto_fix_status = "skipped_deadline"
        router = _make_success_router(nets_routed=3, nets_to_route=3)

        with ExitStack() as stack:
            _patch_routing_engine_for_success(stack, router)
            # DRC reports violations so auto-fix would normally be invoked.
            stack.enter_context(
                patch(
                    "kicad_tools.cli.route_cmd.run_post_route_drc",
                    return_value=(5, 0),
                )
            )

            # Auto-fix is "called" but returns 1 (skipped) AND leaves
            # args._auto_fix_status as "skipped_deadline".  We model
            # this by patching the helper to a function that preserves
            # the pre-set status field.
            def _mock_skipped(output_path, max_passes, quiet, args=None):
                # Simulate the real _run_auto_fix skip path.
                return 1

            stack.enter_context(
                patch("kicad_tools.cli.route_cmd._run_auto_fix", side_effect=_mock_skipped)
            )
            result = route_with_layer_escalation(pcb, out, args, quiet=True)

        # Issue #3238: the skipped-by-deadline status must produce
        # exit code 7, not exit 0 (which would silently hide the skip).
        assert result == 7, (
            f"Expected exit 7 (auto-fix skipped by deadline, issue #3238), "
            f"got {result}.  This is the regression guard against silent "
            f"skip-on-deadline failures (chorus regression mechanism)."
        )
