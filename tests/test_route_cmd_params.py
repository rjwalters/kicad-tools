"""Tests for route command parameter handling (issue #418).

Verifies that CLI parameters like --grid and --clearance are correctly
passed through the command handler to the underlying route_cmd.main().
"""

from types import SimpleNamespace
from unittest.mock import patch


class TestRouteCommandGridParameter:
    """Tests for --grid parameter handling in route command."""

    def test_grid_parameter_passed_when_not_default(self):
        """Grid parameter is passed when different from default 0.25."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating --grid 0.1 (non-default)
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.1",  # Non-default value (string, as parser now emits)
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --grid 0.1 is in the arguments
            call_args = mock_main.call_args[0][0]
            assert "--grid" in call_args
            grid_idx = call_args.index("--grid")
            assert call_args[grid_idx + 1] == "0.1"

    def test_grid_parameter_not_duplicated_when_default(self):
        """Grid parameter is not passed when equal to default 0.25."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating default --grid 0.25
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.25",  # Default value (string) - should not be passed
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --grid is NOT in the arguments (uses default)
            call_args = mock_main.call_args[0][0]
            assert "--grid" not in call_args

    def test_grid_auto_passed_through(self):
        """Grid 'auto' value is always passed through to route_cmd."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="auto",  # Auto mode
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--grid" in call_args
            grid_idx = call_args.index("--grid")
            assert call_args[grid_idx + 1] == "auto"

    def test_grid_auto_uppercase_passed_through(self):
        """Grid 'AUTO' (uppercase) value is passed through to route_cmd."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="AUTO",  # Auto mode uppercase
            trace_width=0.2,
            clearance=0.15,
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--grid" in call_args
            grid_idx = call_args.index("--grid")
            assert call_args[grid_idx + 1] == "AUTO"


class TestRouteCommandClearanceParameter:
    """Tests for --clearance parameter handling in route command."""

    def test_clearance_parameter_passed_when_not_default(self):
        """Clearance parameter is passed when different from default 0.15."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating --clearance 0.127 (non-default)
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.25",
            trace_width=0.2,
            clearance=0.127,  # Non-default value
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --clearance 0.127 is in the arguments
            call_args = mock_main.call_args[0][0]
            assert "--clearance" in call_args
            clearance_idx = call_args.index("--clearance")
            assert call_args[clearance_idx + 1] == "0.127"

    def test_clearance_parameter_not_duplicated_when_default(self):
        """Clearance parameter is not passed when equal to default 0.15."""
        from kicad_tools.cli.commands.routing import run_route_command

        # Create args simulating default --clearance 0.15
        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="0.25",
            trace_width=0.2,
            clearance=0.15,  # Default value - should not be passed
            via_drill=0.3,
            via_diameter=0.6,
            mc_trials=10,
            iterations=15,
            verbose=False,
            dry_run=True,
            quiet=True,
            power_nets=None,
        )

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            # Verify --clearance is NOT in the arguments (uses default)
            call_args = mock_main.call_args[0][0]
            assert "--clearance" not in call_args


class TestRouteCommandDefaultConsistency:
    """Tests to verify default values are consistent across modules."""

    def test_parser_defaults_match_handler_checks(self):
        """Verify parser defaults match the values checked in routing.py handler."""

        from kicad_tools.cli.parser import create_parser

        parser = create_parser()

        # Extract route subparser defaults
        # We need to parse with empty args to get defaults
        args = parser.parse_args(["route", "test.kicad_pcb", "--dry-run"])

        # These should match the checks in routing.py
        assert args.grid == "0.25", "Grid default should be '0.25'"
        assert args.clearance == 0.15, "Clearance default should be 0.15"
        assert args.trace_width == 0.2, "Trace width default should be 0.2"
        assert args.via_drill == 0.3, "Via drill default should be 0.3"
        assert args.via_diameter == 0.6, "Via diameter default should be 0.6"

    def test_parser_accepts_grid_auto(self):
        """Top-level parser accepts --grid auto without error."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--grid", "auto"])
        assert args.grid == "auto", "Parser should accept 'auto' as a grid value"

    def test_parser_accepts_grid_numeric(self):
        """Top-level parser accepts --grid with numeric values as strings."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--grid", "0.1"])
        assert args.grid == "0.1", "Parser should accept '0.1' as a grid value"

    def test_route_cmd_defaults_match_parser_defaults(self):
        """Verify route_cmd.py defaults match parser.py defaults."""
        import contextlib
        import sys
        from io import StringIO
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        # Parse with just the PCB file to get defaults
        # We can't easily test this without mocking, so we verify
        # the argparse setup in route_cmd matches what we expect
        # by checking the help text contains the right defaults
        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()

        # Verify the defaults in help text
        assert "default: 0.25" in help_text, "route_cmd --grid default should be 0.25"
        assert "default: 0.15" in help_text, "route_cmd --clearance default should be 0.15"


class TestRouteCommandGridClearanceValidation:
    """Tests for grid vs clearance validation (issue #529).

    When grid resolution exceeds clearance, the router will create DRC violations.
    The route command should fail unless --force is specified.
    """

    def test_grid_exceeds_clearance_fails_without_force(self, tmp_path):
        """Route command fails when grid > clearance without --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.25, clearance=0.127 → grid > clearance → should fail
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.25",
                "--clearance",
                "0.127",
                "--dry-run",
                "--quiet",
            ]
        )

        assert result == 1, "Should return 1 (error) when grid > clearance"

    def test_grid_exceeds_clearance_succeeds_with_force(self, tmp_path):
        """Route command continues when grid > clearance with --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.25, clearance=0.127 with --force → should continue
        # Note: May still fail for other reasons (empty PCB), but should pass validation
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.25",
                "--clearance",
                "0.127",
                "--force",
                "--dry-run",
                "--quiet",
            ]
        )

        # With --force, it should pass the validation step.
        # It may fail later due to empty PCB, but error code won't be 1
        # (validation error). We check it gets past validation.
        # A minimal PCB with no nets will return 0 with dry-run
        assert result != 1 or result == 0, "Should pass grid/clearance validation with --force"

    def test_grid_within_clearance_succeeds_without_force(self, tmp_path):
        """Route command succeeds when grid <= clearance without --force."""
        from kicad_tools.cli.route_cmd import main as route_main

        # Create a minimal test PCB file
        pcb_content = """(kicad_pcb (version 20240101) (generator "test"))"""
        test_pcb = tmp_path / "test.kicad_pcb"
        test_pcb.write_text(pcb_content)

        # grid=0.1, clearance=0.15 → grid < clearance → should succeed
        result = route_main(
            [
                str(test_pcb),
                "--grid",
                "0.1",
                "--clearance",
                "0.15",
                "--dry-run",
                "--quiet",
            ]
        )

        # Should not fail due to grid/clearance validation
        # (may have other issues with minimal PCB, but won't be validation error)
        assert result == 0, "Should succeed when grid <= clearance"

    def test_force_flag_in_help(self):
        """Verify --force flag is documented in help text."""
        import contextlib
        import sys
        from io import StringIO
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()

        assert "--force" in help_text, "Help text should document --force flag"
        assert "clearance" in help_text.lower(), "Help text should mention clearance"


class TestRouteCommandAutoFixFlags:
    """Tests for --skip-drc, --auto-fix, and --auto-fix-passes forwarding via centralized CLI."""

    def _make_base_args(self, **overrides):
        """Create a base args namespace with all required route fields."""
        defaults = {
            "pcb": "test.kicad_pcb",
            "output": None,
            "strategy": "negotiated",
            "skip_nets": None,
            "grid": "0.25",
            "trace_width": 0.2,
            "clearance": 0.15,
            "via_drill": 0.3,
            "via_diameter": 0.6,
            "mc_trials": 10,
            "iterations": 15,
            "verbose": False,
            "dry_run": True,
            "quiet": True,
            "power_nets": None,
            "layers": "auto",
            "force": False,
            "no_optimize": False,
            "auto_layers": False,
            "max_layers": 6,
            "min_completion": 0.95,
            "adaptive_rules": False,
            "min_trace": None,
            "min_clearance_floor": None,
            "manufacturer": "jlcpcb",
            "high_performance": False,
            "skip_drc": False,
            "auto_fix": False,
            "auto_fix_passes": None,
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    def test_skip_drc_forwarded(self):
        """--skip-drc is forwarded to route_cmd.main."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(skip_drc=True)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--skip-drc" in call_args

    def test_skip_drc_not_forwarded_when_false(self):
        """--skip-drc is not forwarded when not set."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(skip_drc=False)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--skip-drc" not in call_args

    def test_auto_fix_forwarded(self):
        """--auto-fix is forwarded to route_cmd.main."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(auto_fix=True)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--auto-fix" in call_args

    def test_auto_fix_not_forwarded_when_false(self):
        """--auto-fix is not forwarded when not set."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(auto_fix=False)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--auto-fix" not in call_args

    def test_auto_fix_passes_forwarded(self):
        """--auto-fix-passes N is forwarded to route_cmd.main."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(auto_fix_passes=5)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--auto-fix-passes" in call_args
            passes_idx = call_args.index("--auto-fix-passes")
            assert call_args[passes_idx + 1] == "5"

    def test_auto_fix_passes_not_forwarded_when_none(self):
        """--auto-fix-passes is not forwarded when None (default)."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(auto_fix_passes=None)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--auto-fix-passes" not in call_args

    def test_all_three_flags_forwarded_together(self):
        """All three flags forwarded when set simultaneously."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._make_base_args(skip_drc=True, auto_fix=True, auto_fix_passes=7)

        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)

            call_args = mock_main.call_args[0][0]
            assert "--skip-drc" in call_args
            assert "--auto-fix" in call_args
            assert "--auto-fix-passes" in call_args
            passes_idx = call_args.index("--auto-fix-passes")
            assert call_args[passes_idx + 1] == "7"


class TestRouteParserAutoFixFlags:
    """Tests for --skip-drc, --auto-fix, --auto-fix-passes in centralized parser."""

    def test_parser_accepts_skip_drc(self):
        """Centralized parser accepts --skip-drc without error."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--skip-drc"])
        assert args.skip_drc is True

    def test_parser_accepts_auto_fix(self):
        """Centralized parser accepts --auto-fix without error."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--auto-fix"])
        assert args.auto_fix is True

    def test_parser_accepts_auto_fix_passes(self):
        """Centralized parser accepts --auto-fix-passes N without error."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--auto-fix-passes", "5"])
        assert args.auto_fix_passes == 5

    def test_parser_auto_fix_passes_default_is_none(self):
        """--auto-fix-passes defaults to None when not provided."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.auto_fix_passes is None

    def test_parser_skip_drc_default_is_false(self):
        """--skip-drc defaults to False when not provided."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.skip_drc is False

    def test_parser_auto_fix_default_is_false(self):
        """--auto-fix defaults to False when not provided."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.auto_fix is False

    def test_route_help_shows_auto_fix_flags(self):
        """kct route --help output includes the three new flags."""
        import contextlib
        from io import StringIO

        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        help_output = StringIO()
        with contextlib.redirect_stdout(help_output), contextlib.suppress(SystemExit):
            parser.parse_args(["route", "--help"])

        help_text = help_output.getvalue()
        assert "--skip-drc" in help_text
        assert "--auto-fix" in help_text
        assert "--auto-fix-passes" in help_text


class TestEscapeRoutingFlag:
    """Tests for --escape-routing / --no-escape-routing CLI flag handling."""

    def test_resolve_escape_routing_default(self):
        """Default: neither flag set returns None (auto-detect)."""
        from kicad_tools.cli.route_cmd import _resolve_escape_routing_flag

        args = SimpleNamespace(escape_routing=None, no_escape_routing=False)
        assert _resolve_escape_routing_flag(args) is None

    def test_resolve_escape_routing_enabled(self):
        """--escape-routing returns True."""
        from kicad_tools.cli.route_cmd import _resolve_escape_routing_flag

        args = SimpleNamespace(escape_routing=True, no_escape_routing=False)
        assert _resolve_escape_routing_flag(args) is True

    def test_resolve_escape_routing_disabled(self):
        """--no-escape-routing returns False."""
        from kicad_tools.cli.route_cmd import _resolve_escape_routing_flag

        args = SimpleNamespace(escape_routing=None, no_escape_routing=True)
        assert _resolve_escape_routing_flag(args) is False

    def test_no_escape_routing_overrides_escape(self):
        """--no-escape-routing takes precedence over --escape-routing."""
        from kicad_tools.cli.route_cmd import _resolve_escape_routing_flag

        args = SimpleNamespace(escape_routing=True, no_escape_routing=True)
        assert _resolve_escape_routing_flag(args) is False

    def test_should_use_escape_routing_forced(self):
        """escape_flag=True forces escape routing on."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _should_use_escape_routing

        router = MagicMock()
        assert _should_use_escape_routing(router, True, quiet=True) is True
        router.detect_dense_packages.assert_not_called()

    def test_should_use_escape_routing_disabled(self):
        """escape_flag=False forces escape routing off."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _should_use_escape_routing

        router = MagicMock()
        assert _should_use_escape_routing(router, False, quiet=True) is False
        router.detect_dense_packages.assert_not_called()

    def test_should_use_escape_routing_auto_with_dense(self):
        """Auto-detect finds dense packages and enables escape routing."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _should_use_escape_routing

        router = MagicMock()
        router.detect_dense_packages.return_value = [MagicMock(ref="U1")]
        assert _should_use_escape_routing(router, None, quiet=True) is True

    def test_should_use_escape_routing_auto_no_dense(self):
        """Auto-detect finds no dense packages and skips escape routing."""
        from unittest.mock import MagicMock

        from kicad_tools.cli.route_cmd import _should_use_escape_routing

        router = MagicMock()
        router.detect_dense_packages.return_value = []
        assert _should_use_escape_routing(router, None, quiet=True) is False

    def test_escape_routing_in_help(self):
        """Verify --escape-routing appears in help text."""
        import contextlib
        import sys
        from io import StringIO
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--escape-routing" in help_text
        assert "--no-escape-routing" in help_text
