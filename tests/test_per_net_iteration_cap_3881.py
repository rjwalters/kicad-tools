"""Tests for the tuned per-net iteration cap (Issue #3881).

``--deterministic-budget`` (Issue #3877/#3538) made chorus routing
*deterministic* but dropped reach 31/51 -> 13/51.  Root cause: the flag pins
the C++ A* per-net cap to the 12M MEMORY backstop, which is effectively
UNBOUNDED per-net.  One hard net (e.g. I2S_BCLK) burned 280s of the 1200s
``--timeout`` and geometric-failure nets fell through to the 10-100x-slower
Python A*, so only ~14 of 51 nets were even attempted before the outer
deadline fired.

The fix (this issue) adds a TUNED per-net iteration cap, distinct from the
memory backstop:

  1. ``--per-net-iterations N`` bounds each net to N node expansions.  The
     effective C++ cap becomes ``min(N, max_search_iterations)``.
  2. A net hitting the tuned cap is a DETERMINISTIC give-up
     (``FAILURE_ITERATION_LIMIT``) and its Python fallback is SKIPPED, so the
     cap is a hard per-net bound -- the next net gets budget.
  3. The cap is an iteration count (load-independent), so routing stays
     reproducible.
  4. ``--deterministic-budget`` defaults the cap so the recipe recovers
     throughput out of the box.

These tests cover the normalization wiring, the CLI forwarding, the effective
cap computation, and the deterministic give-up / fallback-skip behavior.  The
end-to-end byte-identical determinism proof for a real board lives in
``test_route_deterministic_budget_load_independence.py``.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest import mock

import pytest

from kicad_tools.cli.route_cmd import (
    DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS,
    DETERMINISTIC_BUDGET_PER_NET_ITERATIONS,
    _normalize_deterministic_budget,
)
from kicad_tools.router.cpp_backend import (
    CppGrid,
    CppPathfinder,
    is_cpp_available,
    router_cpp,
)
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad
from kicad_tools.router.rules import DesignRules

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

CPP_BACKEND_LOGGER = "kicad_tools.router.cpp_backend"


def _base_args(**overrides) -> SimpleNamespace:
    args = SimpleNamespace(
        deterministic_budget=False,
        per_net_timeout=30.0,
        max_search_iterations=0,
        per_net_iterations=0,
        timeout=None,
        quiet=True,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class TestNormalizeDefaultsPerNetCap:
    """``--deterministic-budget`` must default the tuned per-net cap."""

    def test_defaults_per_net_cap_under_flag(self):
        args = _base_args(deterministic_budget=True, per_net_iterations=0)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.per_net_iterations == DETERMINISTIC_BUDGET_PER_NET_ITERATIONS
        assert args.per_net_iterations > 0

    def test_per_net_cap_is_smaller_than_memory_backstop(self):
        """The tuned cap must be strictly below the 12M memory backstop, else
        it is not actually bounding hard nets (the whole point of #3881)."""
        assert DETERMINISTIC_BUDGET_PER_NET_ITERATIONS < DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS

    def test_honours_explicit_per_net_override(self):
        args = _base_args(deterministic_budget=True, per_net_iterations=250_000)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.per_net_iterations == 250_000

    def test_noop_when_flag_unset(self):
        """No per-net cap is applied when --deterministic-budget is off."""
        args = _base_args(deterministic_budget=False, per_net_iterations=0)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.per_net_iterations == 0

    def test_memory_backstop_still_pinned(self):
        """The memory backstop is unchanged: the per-net cap is ADDITIVE, not
        a replacement for the 12M ceiling."""
        args = _base_args(deterministic_budget=True)
        _normalize_deterministic_budget(args, quiet=True)
        assert args.max_search_iterations == DETERMINISTIC_BUDGET_MAX_SEARCH_ITERATIONS
        assert args.per_net_iterations == DETERMINISTIC_BUDGET_PER_NET_ITERATIONS

    def test_per_net_cap_is_machine_independent(self):
        """Two independent normalizations land the IDENTICAL per-net cap (the
        determinism guarantee: an integer iteration count, not wall-clock)."""
        a = _base_args(deterministic_budget=True)
        b = _base_args(deterministic_budget=True)
        _normalize_deterministic_budget(a, quiet=True)
        _normalize_deterministic_budget(b, quiet=True)
        assert a.per_net_iterations == b.per_net_iterations


class TestPerNetCapForwarding:
    """``--per-net-iterations`` must flow through the two-parser CLI shim."""

    def _args(self, **overrides) -> SimpleNamespace:
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
            per_net_iterations=0,
        )
        for k, v in overrides.items():
            setattr(args, k, v)
        return args

    def test_forwarded_when_set(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._args(per_net_iterations=750_000)
        with mock.patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            sub_argv = mock_main.call_args[0][0]
        assert "--per-net-iterations" in sub_argv
        idx = sub_argv.index("--per-net-iterations")
        assert sub_argv[idx + 1] == "750000"

    def test_not_forwarded_when_default(self):
        from kicad_tools.cli.commands.routing import run_route_command

        args = self._args(per_net_iterations=0)
        with mock.patch("kicad_tools.cli.route_cmd.main") as mock_main:
            mock_main.return_value = 0
            run_route_command(args)
            sub_argv = mock_main.call_args[0][0]
        assert "--per-net-iterations" not in sub_argv

    def test_flag_on_both_parsers(self):
        """``--per-net-iterations`` lives on both inner and outer parsers."""
        import argparse

        from kicad_tools.cli.parser import create_parser
        from kicad_tools.cli.route_cmd import main as route_main

        # Outer parser flags.
        main_parser = create_parser()
        outer: set[str] = set()
        for action in main_parser._actions:
            choices = getattr(action, "choices", None)
            if choices and "route" in choices:
                for sub_action in choices["route"]._actions:
                    outer.update(sub_action.option_strings)
        assert "--per-net-iterations" in outer

        # Inner parser flags.
        captured: dict[str, argparse.ArgumentParser] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def fake_parse_args(self, *a, **kw):
            if getattr(self, "prog", "") == "kicad-tools route":
                captured["parser"] = self
                raise SystemExit(0)
            return real_parse_args(self, *a, **kw)

        with mock.patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
            with pytest.raises(SystemExit):
                route_main([])
        inner: set[str] = set()
        for action in captured["parser"]._actions:
            inner.update(action.option_strings)
        assert "--per-net-iterations" in inner


def _make_pathfinder(per_net_iterations: int = 0, max_search_iterations: int = 0):
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.2,
        via_drill=0.35,
        via_diameter=0.6,
        via_clearance=0.2,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(
        width=10.0,
        height=10.0,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )
    cpp_grid = CppGrid.from_routing_grid(grid)
    pathfinder = CppPathfinder(
        cpp_grid,
        rules,
        diagonal_routing=True,
        per_net_iterations=per_net_iterations,
        max_search_iterations=max_search_iterations,
    )
    pathfinder.set_routable_layers(cpp_grid.get_routable_indices())
    return pathfinder, grid


def _make_pads(net: int = 1, net_name: str = "NET1") -> tuple[Pad, Pad]:
    start = Pad(x=2.0, y=5.0, width=0.6, height=0.6, net=net, net_name=net_name, layer=Layer.F_CU)
    end = Pad(x=8.0, y=5.0, width=0.6, height=0.6, net=net, net_name=net_name, layer=Layer.F_CU)
    return start, end


@requires_cpp
class TestEffectiveCapComputation:
    """The effective C++ cap = min(per_net_iterations, memory backstop)."""

    def test_no_cap_when_unset(self):
        pf, _ = _make_pathfinder(per_net_iterations=0, max_search_iterations=0)
        assert pf._per_net_iteration_cap_active is False
        assert pf._effective_search_iterations == 0

    def test_per_net_cap_binds_when_set(self):
        pf, _ = _make_pathfinder(per_net_iterations=500_000, max_search_iterations=0)
        assert pf._per_net_iteration_cap_active is True
        assert pf._effective_search_iterations == 500_000

    def test_per_net_cap_clamped_by_memory_backstop(self):
        """When both are set, the smaller (per-net) binds."""
        pf, _ = _make_pathfinder(per_net_iterations=500_000, max_search_iterations=12_000_000)
        assert pf._per_net_iteration_cap_active is True
        assert pf._effective_search_iterations == 500_000

    def test_memory_backstop_binds_when_smaller(self):
        """Defensive: a per-net cap larger than the backstop is clamped down."""
        pf, _ = _make_pathfinder(per_net_iterations=20_000_000, max_search_iterations=12_000_000)
        assert pf._effective_search_iterations == 12_000_000

    def test_memory_backstop_only_when_no_per_net_cap(self):
        pf, _ = _make_pathfinder(per_net_iterations=0, max_search_iterations=12_000_000)
        assert pf._per_net_iteration_cap_active is False
        assert pf._effective_search_iterations == 12_000_000


@requires_cpp
class TestCappedNetSkipsPythonFallback:
    """A tuned-cap give-up is deterministic: the Python fallback is skipped."""

    def test_iteration_limit_skips_fallback_when_cap_active(self):
        pf, _ = _make_pathfinder(per_net_iterations=500_000)
        start, end = _make_pads(net_name="CAPPED_NET")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            result = pf._try_python_fallback(
                start,
                end,
                reason="iteration limit reached",
                cpp_failure_reason=int(router_cpp.FAILURE_ITERATION_LIMIT),
            )

        assert result is None, (
            "issue #3881: a tuned per-net cap give-up must fail the net fast "
            "(return None), not grind in the Python A*."
        )
        router_cls.assert_not_called()

    def test_iteration_limit_falls_back_when_cap_inactive(self):
        """Without a tuned cap, a FAILURE_ITERATION_LIMIT is the 12M MEMORY
        backstop firing -- the legacy #3456 fallback still runs so genuine
        dense escapes are not silently dropped."""
        pf, _ = _make_pathfinder(per_net_iterations=0)
        start, end = _make_pads(net_name="BACKSTOP_NET")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pf._try_python_fallback(
                start,
                end,
                reason="iteration limit reached",
                cpp_failure_reason=int(router_cpp.FAILURE_ITERATION_LIMIT),
            )

        router_cls.assert_called_once()

    def test_geometric_failures_still_fall_back_with_cap_active(self):
        """Even with a cap active, a genuine geometric failure
        (FAILURE_NO_PATH) still uses the Python fallback -- the skip is
        ITERATION_LIMIT-specific."""
        pf, _ = _make_pathfinder(per_net_iterations=500_000)
        start, end = _make_pads(net_name="GEOM_NET")

        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            router_cls.return_value.route.return_value = None
            pf._try_python_fallback(
                start,
                end,
                reason="no path",
                cpp_failure_reason=int(router_cpp.FAILURE_NO_PATH),
            )

        router_cls.assert_called_once()

    def test_capped_skip_does_not_pollute_fallback_stats(self):
        pf, _ = _make_pathfinder(per_net_iterations=500_000)
        start, end = _make_pads(net_name="CAPPED_STATS")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            pf._try_python_fallback(
                start,
                end,
                reason="iteration limit reached",
                cpp_failure_reason=int(router_cpp.FAILURE_ITERATION_LIMIT),
            )

        stats = pf.fallback_stats
        assert "CAPPED_STATS" not in stats["fallback_reasons"]
        assert "CAPPED_STATS" not in stats["fallback_nets"]
        assert stats["fallback_count"] == 0

    def test_capped_skip_logs_debug_not_warning(self, caplog):
        pf, _ = _make_pathfinder(per_net_iterations=500_000)
        start, end = _make_pads(net_name="CAPPED_QUIET")

        with mock.patch("kicad_tools.router.pathfinder.Router"):
            with caplog.at_level(logging.DEBUG, logger=CPP_BACKEND_LOGGER):
                pf._try_python_fallback(
                    start,
                    end,
                    reason="iteration limit reached",
                    cpp_failure_reason=int(router_cpp.FAILURE_ITERATION_LIMIT),
                )

        warnings = [
            rec
            for rec in caplog.records
            if rec.levelno == logging.WARNING and "falling back" in rec.getMessage()
        ]
        assert warnings == [], (
            "a deterministic per-net-cap give-up must not emit the misleading "
            "'falling back' WARNING."
        )
        debug = [
            rec.getMessage()
            for rec in caplog.records
            if rec.levelno == logging.DEBUG and "#3881" in rec.getMessage()
        ]
        assert any("CAPPED_QUIET" in m for m in debug)


@requires_cpp
class TestDeterministicGiveUpEndToEnd:
    """A tiny per-net cap forces a deterministic give-up on a real C++ search.

    With a 1-iteration cap, even a trivial open-field route cannot complete
    within the budget, so the C++ A* aborts with FAILURE_ITERATION_LIMIT and
    -- because the cap is active -- the net fails fast with NO Python
    fallback.  Two runs give the IDENTICAL outcome (load-independent).
    """

    def test_tiny_cap_forces_deterministic_give_up(self):
        pf, _ = _make_pathfinder(per_net_iterations=1)
        start, end = _make_pads(net_name="TINY_CAP")

        # The Python fallback must never run, so patching it to explode proves
        # the give-up is purely the C++ iteration cap.
        with mock.patch("kicad_tools.router.pathfinder.Router") as router_cls:
            route = pf.route(start, end)

        assert route is None, "a 1-iteration cap cannot complete any route"
        router_cls.assert_not_called()

        info = pf.get_last_failure_info()
        assert info is not None
        assert info["failure_reason"] == int(router_cpp.FAILURE_ITERATION_LIMIT)

    def test_give_up_is_reproducible(self):
        """Two pathfinders with the same tiny cap fail at the SAME iteration
        count -- the load-independence invariant in miniature."""
        results = []
        for _ in range(2):
            pf, _ = _make_pathfinder(per_net_iterations=1)
            start, end = _make_pads(net_name="REPRO")
            with mock.patch("kicad_tools.router.pathfinder.Router"):
                route = pf.route(start, end)
            info = pf.get_last_failure_info()
            results.append((route, info["failure_reason"], info["iterations"]))

        assert results[0] == results[1], (
            "the per-net cap is an iteration count, so the give-up must be "
            "byte-for-byte reproducible run-to-run."
        )
