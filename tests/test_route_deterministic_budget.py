"""Tests for the ``--deterministic-budget`` iteration-budgeted routing mode (Issue #3538).

The board-07 Match-Group routing-regression CI gate re-routes from source
and its DRC count must be reproducible across machines so the allowlist
floor in ``.github/routed-drc-tolerance.yml`` can be an exact value
instead of a machine-variance band (the 21 -> 28 -> 34 -> ... treadmill).

The only routing stage that lands a load-dependent amount of copper is the
per-net A* search: on a slow/loaded runner the per-net wall-clock budget
(``--per-net-timeout``) cuts a search short, so the SAME code at the SAME
``--seed`` reaches fewer nets and reports a different DRC profile (the
"#3466 wall-clock-budget cliff").

``--deterministic-budget`` removes that coupling by:
  1. Disabling the per-net wall-clock cutoff (``per_net_timeout = 0``).
  2. Pinning the C++ A* iteration backstop (``max_search_iterations``) to a
     fixed node-expansion count, so each search either finds a path or
     aborts after the SAME number of node expansions on EVERY environment.

These tests prove the normalization wiring is correct (the binding budget
becomes iteration-count, not wall-clock) and that the flag is forwarded
end-to-end through the two-parser CLI architecture.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.cli.route_cmd import (
    DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS,
    _normalize_deterministic_budget,
)


def _base_args(**overrides) -> SimpleNamespace:
    """Build a minimal args namespace for normalization tests."""
    args = SimpleNamespace(
        deterministic_budget=False,
        per_net_timeout=30.0,
        max_search_iterations=0,
        timeout=None,
        quiet=True,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class TestNormalizeDeterministicBudget:
    """Unit tests for ``_normalize_deterministic_budget``."""

    def test_noop_when_flag_unset(self):
        """Legacy behaviour is preserved bit-for-bit when the flag is off."""
        args = _base_args(deterministic_budget=False, per_net_timeout=30.0)
        _normalize_deterministic_budget(args, quiet=True)
        # Nothing changes -- wall-clock per-net budget and 0-backstop intact.
        assert args.per_net_timeout == 30.0
        assert args.max_search_iterations == 0

    def test_disables_per_net_wall_clock_cutoff(self):
        """The per-net wall-clock cutoff is disabled (0.0) under the flag.

        This is the core fix: a slow machine no longer cuts a per-net A*
        short and lands less copper than a fast machine.
        """
        args = _base_args(deterministic_budget=True, per_net_timeout=30.0)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.per_net_timeout == 0.0

    def test_pins_iteration_backstop_to_fixed_value(self):
        """When no explicit backstop is set, the fixed default is pinned.

        A positive backstop is what makes each per-net search terminate
        after a fixed node-expansion count once the wall-clock cutoff is
        removed -- the machine-independent abort point.
        """
        args = _base_args(deterministic_budget=True, max_search_iterations=0)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.max_search_iterations == DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS
        assert args.max_search_iterations > 0

    def test_honours_explicit_backstop_override(self):
        """An explicit positive --max-search-iterations is used verbatim."""
        args = _base_args(deterministic_budget=True, max_search_iterations=500_000)
        _normalize_deterministic_budget(args, quiet=True)
        # User's explicit value wins over the fixed default.
        assert args.max_search_iterations == 500_000

    def test_fixed_backstop_is_machine_independent(self):
        """Two independent normalizations land the IDENTICAL binding budget.

        This is the determinism guarantee in miniature: the budget that
        bounds the work is a fixed integer (node-expansion count), NOT a
        wall-clock value that varies with machine speed/load.  Running the
        normalization on two different "machines" (simulated by two fresh
        arg namespaces) yields byte-identical routing budgets.
        """
        fast_machine = _base_args(deterministic_budget=True)
        slow_machine = _base_args(deterministic_budget=True)
        _normalize_deterministic_budget(fast_machine, quiet=True)
        _normalize_deterministic_budget(slow_machine, quiet=True)

        # The binding budget (iteration count) is identical, and the
        # wall-clock per-net cutoff is disabled on both -- so neither
        # machine's speed can change the amount of work done.
        assert fast_machine.max_search_iterations == slow_machine.max_search_iterations
        assert fast_machine.per_net_timeout == slow_machine.per_net_timeout == 0.0

    def test_warns_when_outer_timeout_set(self, capsys):
        """A set --timeout emits a determinism-breaking-backstop warning."""
        args = _base_args(deterministic_budget=True, timeout=600.0, quiet=False)
        _normalize_deterministic_budget(args, quiet=False)
        out = capsys.readouterr().out
        assert "deterministic-budget" in out
        assert "WARNING" in out
        assert "safety" in out.lower()
        # The outer timeout is retained (safety backstop), not zeroed.
        assert args.timeout == 600.0

    def test_no_warning_when_outer_timeout_unset(self, capsys):
        """No outer-timeout warning when --timeout is unset."""
        args = _base_args(deterministic_budget=True, timeout=None, quiet=False)
        _normalize_deterministic_budget(args, quiet=False)
        out = capsys.readouterr().out
        assert "WARNING" not in out

    def test_quiet_suppresses_output(self, capsys):
        """Quiet mode emits no diagnostics."""
        args = _base_args(deterministic_budget=True, timeout=600.0, quiet=True)
        _normalize_deterministic_budget(args, quiet=True)
        assert capsys.readouterr().out == ""


class TestDeterministicBudgetForwarding:
    """The flag must flow end-to-end through the two-parser CLI shim."""

    def test_flag_forwarded_to_inner_parser(self):
        """``--deterministic-budget`` is appended to the inner sub_argv."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="auto",
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
            deterministic_budget=True,
        )
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            sub_argv = mock_main.call_args[0][0]
            assert "--deterministic-budget" in sub_argv

    def test_flag_not_forwarded_when_unset(self):
        """The flag is omitted from sub_argv when not requested (default)."""
        from kicad_tools.cli.commands.routing import run_route_command

        args = SimpleNamespace(
            pcb="test.kicad_pcb",
            output=None,
            strategy="negotiated",
            skip_nets=None,
            grid="auto",
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
            deterministic_budget=False,
        )
        with patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            sub_argv = mock_main.call_args[0][0]
            assert "--deterministic-budget" not in sub_argv


class TestDeterministicBudgetParserDeclaration:
    """The flag must be declared on BOTH parsers (drift guard, #3538)."""

    def _outer_route_flags(self) -> set[str]:
        from kicad_tools.cli.parser import create_parser

        main_parser = create_parser()
        for action in main_parser._actions:
            choices = getattr(action, "choices", None)
            if choices and "route" in choices:
                flags: set[str] = set()
                for sub_action in choices["route"]._actions:
                    flags.update(sub_action.option_strings)
                return flags
        raise AssertionError("could not find 'route' subparser")

    def _inner_route_flags(self) -> set[str]:
        from kicad_tools.cli.route_cmd import main as route_main

        captured: dict[str, argparse.ArgumentParser] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def fake_parse_args(self, *a, **kw):
            if getattr(self, "prog", "") == "kicad-tools route":
                captured["parser"] = self
                raise SystemExit(0)
            return real_parse_args(self, *a, **kw)

        with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
            with pytest.raises(SystemExit):
                route_main([])
        flags: set[str] = set()
        for action in captured["parser"]._actions:
            flags.update(action.option_strings)
        return flags

    def test_flag_on_both_parsers(self):
        """``--deterministic-budget`` lives on both inner and outer parsers."""
        assert "--deterministic-budget" in self._outer_route_flags()
        assert "--deterministic-budget" in self._inner_route_flags()
