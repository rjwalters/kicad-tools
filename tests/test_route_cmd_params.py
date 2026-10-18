"""Tests for route command parameter handling (issue #418).

Verifies that CLI parameters like --grid and --clearance reach the
underlying route_cmd.main() -- since issue #7017 ``kct route`` hands its argv
over verbatim, so these check the passthrough and the real route parser.
"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.cli import main as cli_main
from tests.route_cli import kct_route, route_args


class TestRouteCommandGridParameter:
    """``kct route --grid`` reaches route_cmd.main verbatim (issues #418, #7017)."""

    @pytest.mark.parametrize("grid", ["0.1", "0.25", "auto", "AUTO"])
    def test_explicit_grid_is_passed_through(self, grid):
        forwarded, args = kct_route(["test.kicad_pcb", "--grid", grid, "--dry-run"])
        assert forwarded[forwarded.index("--grid") + 1] == grid
        assert args.grid == grid

    def test_unset_grid_is_not_invented(self):
        forwarded, args = kct_route(["test.kicad_pcb", "--dry-run"])
        assert "--grid" not in forwarded
        assert args.grid == "auto"


class TestRouteCommandClearanceParameter:
    """``kct route --clearance`` reaches route_cmd.main verbatim."""

    @pytest.mark.parametrize("value", ["0.127", "0.15"])
    def test_explicit_clearance_is_passed_through(self, value):
        """Includes the 0.15 default the old shim dropped (#4875, #7017)."""
        forwarded, args = kct_route(["test.kicad_pcb", "--clearance", value])
        assert forwarded[forwarded.index("--clearance") + 1] == value
        assert args.clearance == pytest.approx(float(value))

    def test_unset_clearance_is_not_invented(self):
        forwarded, _ = kct_route(["test.kicad_pcb"])
        assert "--clearance" not in forwarded


class TestRouteCommandDefaultConsistency:
    """Tests to verify default values are consistent across modules."""

    def test_parser_defaults_match_handler_checks(self):
        """Verify parser defaults match the values checked in routing.py handler."""

        from kicad_tools.cli import route_cmd

        # Extract route subparser defaults
        # We need to parse with empty args to get defaults
        args = route_args(["test.kicad_pcb", "--dry-run"])

        # These should match the checks in routing.py
        assert args.grid == "auto", "Grid default should be 'auto'"
        assert args.clearance == 0.15, "Clearance default should be 0.15"
        # Issue #4700: --trace-width carries a ``None`` sentinel so the command
        # can tell "unset" from an explicit 0.2 and raise the default to the
        # manufacturer's own minimum trace width.  The effective default is
        # still 0.2 (see DEFAULT_TRACE_WIDTH_MM / _effective_trace_width).
        assert args.trace_width is None, "Trace width default should be the None sentinel"
        assert route_cmd.DEFAULT_TRACE_WIDTH_MM == 0.2, "Effective trace width default is 0.2"
        assert route_cmd._effective_trace_width(args) == 0.2
        assert args.via_drill == 0.3, "Via drill default should be 0.3"
        assert args.via_diameter == 0.6, "Via diameter default should be 0.6"

    def test_parser_accepts_grid_auto(self):
        """Top-level parser accepts --grid auto without error."""

        args = route_args(["test.kicad_pcb", "--grid", "auto"])
        assert args.grid == "auto", "Parser should accept 'auto' as a grid value"

    def test_parser_accepts_grid_numeric(self):
        """Top-level parser accepts --grid with numeric values as strings."""

        args = route_args(["test.kicad_pcb", "--grid", "0.1"])
        assert args.grid == "0.1", "Parser should accept '0.1' as a grid value"

    def test_route_cmd_defaults_match_parser_defaults(self):
        """Verify route_cmd.py defaults match parser.py defaults."""
        import contextlib
        import sys
        from io import StringIO

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
        assert "default: auto" in help_text, "route_cmd --grid default should be auto"
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
        pcb_content = (
            pcb_content.rstrip()[:-1]
            + '(gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") (width 0.05)))'
        )
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
        pcb_content = (
            pcb_content.rstrip()[:-1]
            + '(gr_rect (start 0 0) (end 100 100) (layer "Edge.Cuts") (width 0.05)))'
        )
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

        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()

        assert "--force" in help_text, "Help text should document --force flag"
        assert "clearance" in help_text.lower(), "Help text should mention clearance"


class TestRouteCommandAutoFixFlags:
    """--skip-drc, --auto-fix, --auto-fix-passes and the #4094 flags via ``kct route``."""

    BOOLEAN_FLAGS = [
        ("--skip-drc", "skip_drc"),
        ("--auto-fix", "auto_fix"),
        ("--monotone-certificate-order", "monotone_certificate_order"),
        ("--cross-package-pair-corridor", "cross_package_pair_corridor"),
        ("--slack-corridor-widening", "slack_corridor_widening"),
    ]

    @pytest.mark.parametrize(("flag", "dest"), BOOLEAN_FLAGS)
    def test_flag_reaches_router_when_set(self, flag, dest):
        forwarded, args = kct_route(["test.kicad_pcb", flag])
        assert flag in forwarded
        assert getattr(args, dest) is True

    @pytest.mark.parametrize(("flag", "dest"), BOOLEAN_FLAGS)
    def test_flag_absent_and_off_when_unset(self, flag, dest):
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert flag not in forwarded
        assert getattr(args, dest) is False

    def test_auto_fix_passes_forwarded(self):
        forwarded, args = kct_route(["test.kicad_pcb", "--auto-fix-passes", "5"])
        assert forwarded[forwarded.index("--auto-fix-passes") + 1] == "5"
        assert args.auto_fix_passes == 5

    def test_auto_fix_passes_unset_is_none(self):
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert "--auto-fix-passes" not in forwarded
        assert args.auto_fix_passes is None

    def test_all_three_flags_forwarded_together(self):
        forwarded, args = kct_route(
            ["test.kicad_pcb", "--skip-drc", "--auto-fix", "--auto-fix-passes", "7"]
        )
        assert forwarded == ["test.kicad_pcb", "--skip-drc", "--auto-fix", "--auto-fix-passes", "7"]
        assert (args.skip_drc, args.auto_fix, args.auto_fix_passes) == (True, True, 7)


class TestRouteParserAutoFixFlags:
    """Tests for --skip-drc, --auto-fix, --auto-fix-passes in centralized parser."""

    def test_parser_accepts_skip_drc(self):
        """Centralized parser accepts --skip-drc without error."""

        args = route_args(["test.kicad_pcb", "--skip-drc"])
        assert args.skip_drc is True

    def test_parser_accepts_auto_fix(self):
        """Centralized parser accepts --auto-fix without error."""

        args = route_args(["test.kicad_pcb", "--auto-fix"])
        assert args.auto_fix is True

    def test_parser_accepts_auto_fix_passes(self):
        """Centralized parser accepts --auto-fix-passes N without error."""

        args = route_args(["test.kicad_pcb", "--auto-fix-passes", "5"])
        assert args.auto_fix_passes == 5

    def test_parser_auto_fix_passes_default_is_none(self):
        """--auto-fix-passes defaults to None when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.auto_fix_passes is None

    def test_parser_skip_drc_default_is_false(self):
        """--skip-drc defaults to False when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.skip_drc is False

    def test_parser_auto_fix_default_is_false(self):
        """--auto-fix defaults to False when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.auto_fix is False

    def test_route_help_shows_auto_fix_flags(self):
        """kct route --help output includes the three new flags."""
        import contextlib
        from io import StringIO

        help_output = StringIO()
        with contextlib.redirect_stdout(help_output), contextlib.suppress(SystemExit):
            cli_main(["route", "--help"])

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

        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--escape-routing" in help_text
        assert "--no-escape-routing" in help_text


class TestRouteCommandNoCacheFlag:
    """Tests for --no-cache flag in subcommand parser and dispatch bridge."""

    def test_parser_accepts_no_cache(self):
        """Centralized parser accepts --no-cache without error."""

        args = route_args(["test.kicad_pcb", "--no-cache"])
        assert args.no_cache is True

    def test_parser_no_cache_default_is_false(self):
        """--no-cache defaults to False when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.no_cache is False

    def test_no_cache_forwarded(self):
        forwarded, args = kct_route(["test.kicad_pcb", "--no-cache"])
        assert "--no-cache" in forwarded
        assert args.no_cache is True

    def test_no_cache_not_forwarded_when_unset(self):
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert "--no-cache" not in forwarded
        assert args.no_cache is False


class TestRouteParserPerNetTimeoutFlag:
    """Tests for --per-net-timeout and --timeout in subcommand parser and dispatch bridge."""

    def test_parser_accepts_per_net_timeout(self):
        """Centralized parser accepts --per-net-timeout without error."""

        args = route_args(["test.kicad_pcb", "--per-net-timeout", "60"])
        assert args.per_net_timeout == 60.0

    def test_parser_per_net_timeout_default_is_30(self):
        """--per-net-timeout defaults to 30.0 when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.per_net_timeout == 30.0

    def test_parser_accepts_per_net_timeout_zero(self):
        """--per-net-timeout 0 is accepted (disables timeout)."""

        args = route_args(["test.kicad_pcb", "--per-net-timeout", "0"])
        assert args.per_net_timeout == 0.0

    def test_parser_accepts_timeout(self):
        """Centralized parser accepts --timeout without error."""

        args = route_args(["test.kicad_pcb", "--timeout", "120"])
        assert args.timeout == 120.0

    def test_parser_timeout_default_is_none(self):
        """--timeout defaults to None when not provided."""

        args = route_args(["test.kicad_pcb"])
        assert args.timeout is None

    @pytest.mark.parametrize(
        ("flag", "value", "dest", "expected"),
        [
            ("--per-net-timeout", "60", "per_net_timeout", 60.0),
            ("--per-net-timeout", "30", "per_net_timeout", 30.0),  # explicit default
            ("--per-net-timeout", "0", "per_net_timeout", 0.0),
            ("--timeout", "120", "timeout", 120.0),
        ],
    )
    def test_timeout_flags_forwarded(self, flag, value, dest, expected):
        forwarded, args = kct_route(["test.kicad_pcb", flag, value])
        assert forwarded[forwarded.index(flag) + 1] == value
        assert getattr(args, dest) == expected

    def test_timeouts_not_forwarded_when_unset(self):
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert "--per-net-timeout" not in forwarded
        assert "--timeout" not in forwarded
        assert args.per_net_timeout == 30.0
        assert args.timeout is None

    def test_route_help_shows_per_net_timeout(self):
        """kct route --help output includes --per-net-timeout and --timeout."""
        import contextlib
        from io import StringIO

        help_output = StringIO()
        with contextlib.redirect_stdout(help_output), contextlib.suppress(SystemExit):
            cli_main(["route", "--help"])

        help_text = help_output.getvalue()
        assert "--per-net-timeout" in help_text
        assert "--timeout" in help_text


class TestManufacturerTraceWidthFloor:
    """Issue #4700: --trace-width resolves from the manufacturer's own floor.

    ``kct route --manufacturer jlcpcb`` used to emit copper below the very
    floor ``kct check --mfr jlcpcb`` enforces, because route floored at the
    layer/copper-agnostic ``MfrLimits.min_trace`` (0.127mm) while check reads
    the per-``<layers>layer_<oz>oz`` ``min_trace_width_mm`` (0.1524mm at 2oz).
    """

    @staticmethod
    def _args(**overrides):
        from argparse import Namespace

        base = {
            "manufacturer": "jlcpcb",
            "copper": None,
            "layers": "4",
            "trace_width": None,
            "min_trace": None,
            "quiet": False,
            "_loaded_net_class_map": None,
        }
        base.update(overrides)
        return Namespace(**base)

    @staticmethod
    def _pcb(tmp_path):
        """A minimal board with no explicit stackup (profile default copper)."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20221018) (generator test)\n)\n")
        return pcb

    def test_unset_width_is_raised_to_the_2oz_floor(self, tmp_path, capsys):
        """max(0.2, floor) with the floor reported once on stdout."""
        from kicad_tools.cli import route_cmd

        args = self._args(copper="2", trace_width=None)
        # A wide-ish explicit-free run: the 0.1524 floor is below 0.2, so the
        # default stands -- but the ladder floor must be raised.
        assert route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path)) == 0
        assert args.trace_width == 0.2
        assert args.min_trace == pytest.approx(0.1524)
        assert args.copper_oz == 2.0

    def test_binding_floor_raises_the_default_and_announces_it(self, tmp_path, capsys):
        """A floor above 0.2 raises the default and prints one summary line."""

        from kicad_tools.cli import route_cmd

        args = self._args(copper="2", trace_width=None)
        with patch.object(route_cmd, "DEFAULT_TRACE_WIDTH_MM", 0.1):
            with patch(
                "kicad_tools.router.mfr_limits.resolve_min_trace_width",
                return_value=0.1524,
            ):
                assert (
                    route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path)) == 0
                )
        assert args.trace_width == pytest.approx(0.1524)
        out = capsys.readouterr().out
        assert "Trace width:" in out
        assert "jlcpcb" in out

    def test_non_binding_floor_leaves_the_default_untouched(self, tmp_path, capsys):
        """jlcpcb 4-layer 1oz floor (0.1016) does not move the 0.2 default."""
        from kicad_tools.cli import route_cmd

        args = self._args()
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))
        assert args.trace_width == 0.2
        assert "Trace width:" not in capsys.readouterr().out

    def test_explicit_sub_floor_width_warns_but_is_honored(self, tmp_path, capsys):
        """The reporter's exact emission: 0.150mm against the 0.1524mm floor."""
        from kicad_tools.cli import route_cmd

        args = self._args(copper="2", trace_width=0.15)
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))

        # Honored, never silently rewritten.
        assert args.trace_width == 0.15
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "0.15" in err
        assert "0.1524" in err
        assert "jlcpcb" in err
        assert "4-layer 2oz" in err
        assert "dimension_trace_width" in err

    def test_explicit_width_at_the_floor_does_not_warn(self, tmp_path, capsys):
        from kicad_tools.cli import route_cmd

        args = self._args(copper="2", trace_width=0.1524)
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))
        assert "WARNING" not in capsys.readouterr().err

    def test_explicit_min_trace_below_the_floor_warns(self, tmp_path, capsys):
        from kicad_tools.cli import route_cmd

        args = self._args(copper="2", min_trace=0.127)
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))
        # User override wins, but the divergence is announced.
        assert args.min_trace == 0.127
        err = capsys.readouterr().err
        assert "--min-trace" in err
        assert "dimension_trace_width" in err

    def test_net_class_widths_are_reported_never_rewritten(self, tmp_path, capsys):
        from kicad_tools.cli import route_cmd
        from kicad_tools.router.rules import NetClassRouting

        ncm = {"DP": NetClassRouting(name="DIFFERENTIAL", trace_width=0.15)}
        args = self._args(copper="2", _loaded_net_class_map=ncm)
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))

        assert ncm["DP"].trace_width == 0.15  # user data untouched
        err = capsys.readouterr().err
        assert "DIFFERENTIAL" in err
        assert "dimension_trace_width" in err

    def test_no_manufacturer_is_a_complete_no_op(self, tmp_path, capsys):
        from kicad_tools.cli import route_cmd

        args = self._args(manufacturer=None, copper=None)
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))
        assert args.trace_width == 0.2
        assert args.min_trace is None
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_unknown_manufacturer_degrades_gracefully(self, tmp_path):
        from kicad_tools.cli import route_cmd

        args = self._args(manufacturer="not-a-fab")
        assert route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path)) == 0
        assert args.trace_width == 0.2
        assert args.min_trace is None

    def test_malformed_copper_exits_1(self, tmp_path, capsys):
        from kicad_tools.cli import route_cmd

        args = self._args(copper="bogus")
        assert route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path)) == 1
        assert "Error:" in capsys.readouterr().err

    def test_keyed_copper_form_parses_like_check(self, tmp_path):
        from kicad_tools.cli import route_cmd
        from kicad_tools.cli.check_cmd import _parse_copper_weight_arg

        assert _parse_copper_weight_arg("outer=2,inner=0.5") == (2.0, 0.5)
        args = self._args(copper="outer=2,inner=0.5")
        route_cmd._apply_manufacturer_trace_width_floor(args, self._pcb(tmp_path))
        assert args.copper_oz == 2.0
        assert args._copper_oz_inner == 0.5
        assert args.min_trace == pytest.approx(0.1524)

    def test_layer_count_drives_the_1oz_floor(self, tmp_path):
        from kicad_tools.cli import route_cmd

        two = self._args(layers="2")
        route_cmd._apply_manufacturer_trace_width_floor(two, self._pcb(tmp_path))
        four = self._args(layers="4-sig")
        route_cmd._apply_manufacturer_trace_width_floor(four, self._pcb(tmp_path))
        assert two.min_trace == pytest.approx(0.127)
        assert four.min_trace == pytest.approx(0.1016)


class TestRouteCopperFlagPlumbing:
    """Issue #4700: `kct route --copper` exists and forwards verbatim."""

    def test_parser_accepts_copper_scalar_and_keyed(self):
        assert route_args(["b.kicad_pcb", "--copper", "2"]).copper == "2"
        keyed = route_args(["b.kicad_pcb", "--copper", "outer=2,inner=0.5"])
        assert keyed.copper == "outer=2,inner=0.5"

    def test_copper_default_is_none(self):
        assert route_args(["b.kicad_pcb"]).copper is None

    def test_route_help_documents_the_floor_behavior(self):
        import contextlib
        from io import StringIO

        help_output = StringIO()
        with contextlib.redirect_stdout(help_output), contextlib.suppress(SystemExit):
            cli_main(["route", "--help"])

        help_text = help_output.getvalue()
        assert "--copper" in help_text
        # The warn-not-error contract is documented in --help per #4700.
        assert "dimension_trace_width" in help_text

    def test_unset_trace_width_is_not_invented(self):
        """The ``None`` sentinel survives: nothing is forwarded for an unset width."""
        forwarded, args = kct_route(["test.kicad_pcb"])
        assert "--trace-width" not in forwarded
        assert args.trace_width is None

    def test_explicit_trace_width_and_copper_pass_through(self):
        forwarded, args = kct_route(["test.kicad_pcb", "--trace-width", "0.15", "--copper", "2"])
        assert forwarded[forwarded.index("--trace-width") + 1] == "0.15"
        assert forwarded[forwarded.index("--copper") + 1] == "2"
        assert args.trace_width == pytest.approx(0.15)
        assert args.copper == "2"
