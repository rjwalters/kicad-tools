"""Tests for the ``--length-match-groups`` CLI flag (Epic #2661 Phase 3H).

Issue #2723.  This module covers the CLI surface around
:meth:`Autorouter.apply_match_group_tuning`:

- **AC2**: ``kct route --help`` lists ``--length-match-groups`` with a
  docstring referencing Epic #2661 Phase 3.
- **AC3**: The flag is defined in BOTH the standalone ``route_cmd.py``
  parser AND the unified ``cli/parser.py`` route subcommand.
- **AC4**: When no groups are detected the CLI does NOT crash -- the
  graceful short-circuit is exercised inline (the actual end-to-end
  invocation lives in unit tests for :meth:`apply_match_group_tuning`
  to keep the CI matrix fast).
- **AC5 ordering**: ``run_route_command`` forwards the flag AFTER
  ``--length-match-diffpairs`` so ordering is preserved end-to-end.
- **Forwarding**: ``run_route_command`` forwards / does NOT forward the
  flag based on the args namespace value (mirrors the
  ``--length-match-diffpairs`` plumbing).
"""

from __future__ import annotations

import contextlib
import sys
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

# =============================================================================
# AC2 + AC3: CLI flag is defined in both parsers
# =============================================================================


class TestFlagDefinedInBothParsers:
    """The flag must appear in both the standalone and unified parsers."""

    def test_length_match_groups_in_route_cmd_help(self):
        """``route_cmd.main(['--help'])`` lists ``--length-match-groups``."""
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])

        help_text = help_output.getvalue()
        assert "--length-match-groups" in help_text
        # AC2: docstring must reference Epic #2661 Phase 3.
        # argparse wraps long help text across lines, so collapse whitespace
        # before searching for the documented strings.
        collapsed = " ".join(help_text.split())
        assert "Epic #2661" in collapsed
        assert "Phase 3H" in collapsed

    def test_length_match_groups_in_unified_parser(self):
        """``kct route --length-match-groups`` is parseable via the unified parser."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb", "--length-match-groups"])
        assert args.length_match_groups is True

    def test_length_match_groups_default_false(self):
        """When omitted, ``length_match_groups`` defaults to False."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "test.kicad_pcb"])
        assert args.length_match_groups is False


# =============================================================================
# Forwarding: run_route_command -> route_cmd.main
# =============================================================================


def _base_args(**overrides) -> SimpleNamespace:
    """Build a minimal args namespace mirroring run_route_command's needs."""
    base: dict[str, object] = {
        "pcb": "test.kicad_pcb",
        "output": None,
        "strategy": "negotiated",
        "skip_nets": None,
        "grid": "auto",
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
        "export_failed_nets": None,
        "differential_pairs": False,
        "diffpair_spacing": None,
        "diffpair_max_delta": None,
        "length_match_diffpairs": False,
        "length_match_groups": False,
        "strict": False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestForwarding:
    """``run_route_command`` forwards the new flag when set, omits it otherwise."""

    def test_length_match_groups_forwarded_when_true(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(length_match_groups=True)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--length-match-groups" in call_args

    def test_length_match_groups_not_forwarded_when_false(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(length_match_groups=False)
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--length-match-groups" not in call_args

    def test_compatible_with_length_match_diffpairs(self):
        """Both flags can be set together; both are forwarded."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(
            differential_pairs=True,
            length_match_diffpairs=True,
            length_match_groups=True,
        )
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            assert "--length-match-diffpairs" in call_args
            assert "--length-match-groups" in call_args

    def test_forwarding_preserves_ordering(self):
        """AC5: ``--length-match-groups`` is forwarded AFTER
        ``--length-match-diffpairs`` so the post-routing dispatch in
        route_cmd.py runs pair tuning first, group tuning second."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = _base_args(
            differential_pairs=True,
            length_match_diffpairs=True,
            length_match_groups=True,
        )
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            call_args = mock_main.call_args[0][0]
            pair_idx = call_args.index("--length-match-diffpairs")
            group_idx = call_args.index("--length-match-groups")
            # The forwarder appends groups AFTER diffpairs; the route_cmd
            # post-routing dispatch then runs them in the order they appear.
            assert pair_idx < group_idx, (
                "Forwarding order must place --length-match-diffpairs before "
                "--length-match-groups so pair tuning runs first (preserves "
                "the within-pair skew invariant before group tuning perturbs "
                "lane lengths)."
            )


# =============================================================================
# AC4: graceful no-op when no groups are detected
# =============================================================================


class TestGracefulNoOp:
    """When no groups are detected the CLI must not crash."""

    def test_no_groups_path_handled_in_route_cmd(self):
        """The route_cmd post-routing block has the explicit
        ``if not detected_groups`` short-circuit so a synthetic-test board
        with no declared groups exits cleanly with no tuning attempted.

        Read the source directly -- a unit-test against the live CLI
        would have to spin up an Autorouter with a routed board, which
        is out of scope for this fast CI-friendly test.
        """
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The literal short-circuit string is the contract.  Drift here
        # means the AC4 graceful-no-op contract is broken silently.
        assert "No match groups detected; nothing to tune." in src, (
            "route_cmd.py must emit a graceful 'no groups detected' "
            "message when detect_match_groups returns empty (AC4)."
        )

    def test_phase_label_matches_connectivity_invariant(self):
        """The connectivity invariant phase label is ``length_match_groups``."""
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The phase label is consumed by _enforce_connectivity_invariant_or_exit
        # and surfaced to users on regression.  AC7 depends on it being
        # consistent with the diff-pair sibling's ``length_match_diffpairs``.
        assert 'phase="length_match_groups"' in src, (
            "route_cmd.py must use phase='length_match_groups' for the "
            "connectivity invariant call (AC7)."
        )


# =============================================================================
# AC1: Without the flag, no tuning runs (preserves backward compat)
# =============================================================================


class TestBackwardCompat:
    """Without ``--length-match-groups``, ``apply_match_group_tuning`` is
    NOT invoked (AC1).
    """

    def test_apply_match_group_tuning_not_invoked_when_flag_absent(self):
        """The route_cmd dispatch is gated on
        ``getattr(args, 'length_match_groups', False)``.  This test asserts
        the gate exists by reading the source -- the same pattern as the
        diff-pair sibling at AC1.  A live test would require a routed
        board, out of scope here.
        """
        import inspect

        from kicad_tools.cli import route_cmd

        src = inspect.getsource(route_cmd)
        # The gate must be present and exact.
        assert 'getattr(args, "length_match_groups", False)' in src, (
            "route_cmd.py must gate the match-group tuning dispatch on "
            "args.length_match_groups (AC1)."
        )
