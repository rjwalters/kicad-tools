"""Regression tests for the ``--order-method`` CLI plumbing (Issue #3897).

This wires the previously-orphaned
``kicad_tools.optim.routing.RoutingOptimizer.optimize_net_order`` into
``kct route`` via a new ``--order-method`` flag with choices
``{greedy, critical_first, congestion, hybrid}``.

The tests pin all three plumbing layers (mirroring the drift-test pattern
from ``tests/test_cli_region_parallel.py``):

1. Outer parser (``cli/parser.py``) declares ``--order-method``.
2. Inner parser (``cli/route_cmd.py``) declares ``--order-method``.
3. Forwarding shim (``cli/commands/routing.py :: run_route_command``)
   forwards it only when set (byte-identical default path otherwise).

Plus behavioural tests for ``_apply_order_method`` (the helper that calls
``optimize_net_order`` and stashes the result on ``router._forced_net_order``)
and a CLI-level spy test asserting ``route_all`` receives a non-None
``net_order`` when ``--order-method greedy`` is threaded through.
"""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers (mirror tests/test_cli_region_parallel.py)
# ---------------------------------------------------------------------------


def _flags_from_parser(parser: argparse.ArgumentParser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:
        for option_string in action.option_strings:
            if option_string.startswith("--"):
                flags.add(option_string)
    return flags


def _inner_route_parser_flags() -> set[str]:
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with pytest.raises(SystemExit):
            route_main([])

    assert "parser" in captured, "failed to capture inner route parser"
    return _flags_from_parser(captured["parser"])


def _outer_route_subparser() -> argparse.ArgumentParser:
    from kicad_tools.cli.parser import create_parser

    main_parser = create_parser()
    for action in main_parser._actions:
        choices = getattr(action, "choices", None)
        if choices and "route" in choices:
            return choices["route"]
    raise AssertionError("could not find 'route' subparser on outer parser")


def _outer_route_parser_flags() -> set[str]:
    return _flags_from_parser(_outer_route_subparser())


# ---------------------------------------------------------------------------
# Layer 1 — outer parser declares --order-method with expected default/choices
# ---------------------------------------------------------------------------


def test_outer_parser_declares_order_method():
    outer = _outer_route_parser_flags()
    assert "--order-method" in outer, (
        "--order-method missing from outer parser (would regress #3897)"
    )


def test_outer_parser_order_method_default_none():
    """Default is None so ordering is byte-identical to legacy behaviour."""
    route_parser = _outer_route_subparser()
    args = route_parser.parse_args(["dummy.kicad_pcb"])
    assert args.order_method is None


@pytest.mark.parametrize("method", ["greedy", "critical_first", "congestion", "hybrid"])
def test_outer_parser_accepts_each_order_method(method):
    route_parser = _outer_route_subparser()
    args = route_parser.parse_args(["dummy.kicad_pcb", "--order-method", method])
    assert args.order_method == method


def test_outer_parser_rejects_unknown_order_method():
    route_parser = _outer_route_subparser()
    with pytest.raises(SystemExit):
        route_parser.parse_args(["dummy.kicad_pcb", "--order-method", "annealing"])


# ---------------------------------------------------------------------------
# Layer 2 — inner parser declares --order-method
# ---------------------------------------------------------------------------


def test_inner_parser_declares_order_method():
    inner = _inner_route_parser_flags()
    assert "--order-method" in inner, (
        "--order-method missing from inner route_cmd.py parser "
        "(would regress #3897 -- shim cannot forward to a non-existent flag)"
    )


# ---------------------------------------------------------------------------
# Layer 3 — shim forwards --order-method only when set
# ---------------------------------------------------------------------------


def _run_shim_capture_argv(extra_argv: list[str]) -> list[str]:
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    main_parser = create_parser()
    args = main_parser.parse_args(["route", "dummy.kicad_pcb", *extra_argv])

    captured: dict[str, list[str]] = {}

    def fake_route_main(sub_argv):
        captured["argv"] = list(sub_argv)
        return 0

    with patch("kicad_tools.cli.route_cmd.main", fake_route_main):
        run_route_command(args)

    assert "argv" in captured, "shim did not call inner route_main"
    return captured["argv"]


def test_shim_omits_order_method_by_default():
    """Default path must not add --order-method (byte-identity guard)."""
    argv = _run_shim_capture_argv([])
    assert "--order-method" not in argv


def test_shim_forwards_order_method_when_set():
    argv = _run_shim_capture_argv(["--order-method", "greedy"])
    idx = argv.index("--order-method")
    assert argv[idx + 1] == "greedy"


@pytest.mark.parametrize("method", ["greedy", "critical_first", "congestion", "hybrid"])
def test_shim_forwards_each_order_method(method):
    argv = _run_shim_capture_argv(["--order-method", method])
    idx = argv.index("--order-method")
    assert argv[idx + 1] == method


# ---------------------------------------------------------------------------
# Behavioural — _apply_order_method computes and stashes the explicit order
# ---------------------------------------------------------------------------


class _FakeRouter:
    """Minimal Autorouter stand-in for the ordering heuristics.

    ``optimize_net_order`` for greedy / critical_first only reads ``nets`` and
    ``net_names``, then evaluates the order with a final ``route_all`` (whose
    result the CLI ignores).  We stub ``route_all`` to a no-op so the helper
    does not need a real grid.
    """

    def __init__(self, nets, net_names, congestion_raises=False):
        self.nets = nets
        self.net_names = net_names
        self._forced_net_order: list[int] | None = None
        self._congestion_raises = congestion_raises
        self.route_all_calls: list[dict] = []

    def route_all(self, net_order=None, **kwargs):
        self.route_all_calls.append({"net_order": net_order, **kwargs})
        return []

    def get_congestion_map(self):
        if self._congestion_raises:
            raise RuntimeError("no grid available")
        # A trivial congestion map: estimate_net_congestion tolerates any
        # object exposing the query surface, but we route congestion tests
        # through the failure path so this stub only needs to raise there.
        raise RuntimeError("congestion map unsupported in this fake")


def test_apply_order_method_noop_when_absent():
    from kicad_tools.cli.route_cmd import _apply_order_method

    router = _FakeRouter({1: [("R1", "1")]}, {1: "NET1"})
    args = SimpleNamespace(order_method=None)
    _apply_order_method(router, args, router_factory=lambda: router, quiet=True)
    assert router._forced_net_order is None


def test_apply_order_method_greedy_sets_forced_order():
    from kicad_tools.cli.route_cmd import _apply_order_method

    # net 3 has 1 pad, net 1 has 2 pads, net 2 has 3 pads -> greedy = [3, 1, 2]
    nets = {
        1: [("R1", "1"), ("R1", "2")],
        2: [("U1", "1"), ("U1", "2"), ("U1", "3")],
        3: [("C1", "1")],
    }
    names = {1: "SIG_A", 2: "SIG_B", 3: "SIG_C"}
    router = _FakeRouter(nets, names)
    args = SimpleNamespace(order_method="greedy")

    _apply_order_method(router, args, router_factory=lambda: router, quiet=True)

    assert router._forced_net_order is not None
    assert all(isinstance(n, int) for n in router._forced_net_order)
    assert router._forced_net_order == [3, 1, 2]


def test_apply_order_method_critical_first_puts_power_nets_first():
    from kicad_tools.cli.route_cmd import _apply_order_method

    nets = {
        1: [("R1", "1"), ("R1", "2")],  # signal
        2: [("U1", "1"), ("U1", "2")],  # power (VCC)
        3: [("C1", "1"), ("C1", "2")],  # power (GND)
    }
    names = {1: "DATA0", 2: "VCC3V3", 3: "GND"}
    router = _FakeRouter(nets, names)
    args = SimpleNamespace(order_method="critical_first")

    _apply_order_method(router, args, router_factory=lambda: router, quiet=True)

    order = router._forced_net_order
    assert order is not None
    # Both power nets (VCC / GND) must precede the signal net.
    assert order.index(2) < order.index(1)
    assert order.index(3) < order.index(1)


def test_apply_order_method_congestion_falls_back_to_greedy(capsys):
    from kicad_tools.cli.route_cmd import _apply_order_method

    nets = {
        1: [("R1", "1"), ("R1", "2")],
        2: [("C1", "1")],
    }
    names = {1: "SIG_A", 2: "SIG_B"}
    # get_congestion_map raises -> helper must warn and fall back to greedy.
    router = _FakeRouter(nets, names, congestion_raises=True)
    args = SimpleNamespace(order_method="congestion")

    _apply_order_method(router, args, router_factory=lambda: router, quiet=False)

    # Greedy fallback: net 2 (1 pad) before net 1 (2 pads).
    assert router._forced_net_order == [2, 1]
    out = capsys.readouterr().out
    assert "congestion" in out
    assert "greedy" in out


# ---------------------------------------------------------------------------
# CLI-level spy — route_all receives a non-None net_order under --order-method
# ---------------------------------------------------------------------------


def _build_real_router():
    """Build a real ``Autorouter`` with two 2-pad nets for override tests."""
    from kicad_tools.router.core import Autorouter

    router = Autorouter(width=30.0, height=30.0)
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 5.0,
                "y": 5.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET_A",
            },
            {
                "number": "2",
                "x": 5.0,
                "y": 15.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET_B",
            },
        ],
    )
    router.add_component(
        "U2",
        [
            {
                "number": "1",
                "x": 20.0,
                "y": 5.0,
                "width": 0.5,
                "height": 0.5,
                "net": 1,
                "net_name": "NET_A",
            },
            {
                "number": "2",
                "x": 20.0,
                "y": 15.0,
                "width": 0.5,
                "height": 0.5,
                "net": 2,
                "net_name": "NET_B",
            },
        ],
    )
    return router


def test_route_all_seeds_base_order_from_forced_order(monkeypatch):
    """Core.py override: ``route_all(net_order=None)`` seeds its base order
    from ``router._forced_net_order`` when set.

    Rather than run the full A* loop, we spy on ``_filter_pour_nets`` (invoked
    immediately after the base ``net_order`` is established) to capture the
    order the router chose.  This confirms the forced order -- not the internal
    ``_get_net_priority`` sort -- drives ``route_all``.
    """
    router = _build_real_router()
    # Force NET_B (id 2) before NET_A (id 1) -- the reverse of what a naive
    # priority sort by pad-count/name would produce for identical 2-pad nets.
    router._forced_net_order = [2, 1]

    captured: dict[str, list[int]] = {}

    real_filter = router._filter_pour_nets

    def spy_filter(net_order):
        captured["net_order"] = list(net_order)
        # Return empty so route_all short-circuits without running A*.
        real_filter(net_order)
        return []

    monkeypatch.setattr(router, "_filter_pour_nets", spy_filter)

    router.route_all(suppress_no_timeout_warning=True)

    assert captured["net_order"] == [2, 1], (
        "route_all did not seed its base order from _forced_net_order"
    )


def test_route_all_default_order_unchanged_without_forced_order(monkeypatch):
    """Byte-identity guard: with ``_forced_net_order`` unset, the base order is
    the legacy ``_get_net_priority`` sort (not perturbed by #3897)."""
    router = _build_real_router()
    assert router._forced_net_order is None

    expected = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))

    captured: dict[str, list[int]] = {}

    def spy_filter(net_order):
        captured["net_order"] = list(net_order)
        return []

    monkeypatch.setattr(router, "_filter_pour_nets", spy_filter)

    router.route_all(suppress_no_timeout_warning=True)

    assert captured["net_order"] == expected
