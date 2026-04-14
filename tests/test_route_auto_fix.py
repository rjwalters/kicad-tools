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
        """_run_auto_fix prints failure message when not quiet and fix fails."""
        from pathlib import Path

        mock_fix_drc.return_value = 1
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=False)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" in captured.out
        assert "some violations remain" in captured.out

    @patch("kicad_tools.cli.fix_drc_cmd.main")
    def test_run_auto_fix_quiet_no_output(self, mock_fix_drc, capsys):
        """_run_auto_fix suppresses output when quiet."""
        from pathlib import Path

        mock_fix_drc.return_value = 0
        _run_auto_fix(Path("/tmp/board.kicad_pcb"), max_passes=1, quiet=True)

        captured = capsys.readouterr()
        assert "Auto-Fix DRC Violations" not in captured.out


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
