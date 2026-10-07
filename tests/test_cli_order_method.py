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


# ---------------------------------------------------------------------------
# Issue #5908 — greedy|critical_first|congestion|hybrid on escalation paths
#
# ``_apply_order_method`` for these four evaluates the candidate order with a
# throw-away full route and needs a fresh-router factory that the escalation
# attempt loops do not build.  They used to be silently discarded on every
# escalation path (including the default ``--auto-layers`` recipe).  The fix
# rejects them with exit code 2 and an actionable stderr message *before* any
# board is loaded, evaluated or routed.  These tests drive the real
# ``route_cmd.main`` dispatch and count every routing consumer.
# ---------------------------------------------------------------------------

_SINGLE_ATTEMPT_ONLY = ["greedy", "critical_first", "congestion", "hybrid"]

_DISPATCH_FUNCS = [
    "route_with_layer_escalation",
    "route_with_rule_relaxation",
    "route_with_combined_escalation",
    "route_with_size_escalation",
    "route_with_mfr_tier_escalation",
]

# (path id, extra argv, dispatch function main() selects for that argv)
_ESCALATION_PATHS = [
    ("layer_escalation_default", [], "route_with_layer_escalation"),
    ("layer_escalation_explicit", ["--auto-layers"], "route_with_layer_escalation"),
    (
        "rule_relaxation",
        ["--no-auto-layers", "--adaptive-rules"],
        "route_with_rule_relaxation",
    ),
    ("combined_escalation", ["--adaptive-rules"], "route_with_combined_escalation"),
    ("size_escalation", ["--auto-pcb-size"], "route_with_size_escalation"),
    (
        "size_escalation_no_auto_layers",
        ["--auto-pcb-size", "--no-auto-layers"],
        "route_with_size_escalation",
    ),
    ("mfr_tier_escalation", ["--auto-mfr-tier"], "route_with_mfr_tier_escalation"),
    (
        "mfr_tier_no_auto_layers",
        ["--auto-mfr-tier", "--no-auto-layers"],
        "route_with_mfr_tier_escalation",
    ),
]


class _CallCounter:
    def __init__(self, name: str, rc=0):
        self.name = name
        self.calls = 0
        self.rc = rc

    def __call__(self, *args, **kwargs):
        self.calls += 1
        return self.rc


def _fixture_board(tmp_path):
    import shutil
    from pathlib import Path

    src = Path(__file__).parent / "fixtures" / "stale_nets.kicad_pcb"
    dst = tmp_path / "board.kicad_pcb"
    shutil.copy(src, dst)
    return dst


class _ApplyReached(BaseException):
    """Raised by the ``_apply_order_method`` spy to stop before the real route.

    ``BaseException`` so ``main``'s broad ``except Exception`` handlers cannot
    swallow it.
    """


def _run_main_with_spies(argv, monkeypatch, *, real_load=False, stop_at_apply=False):
    """Run ``route_cmd.main(argv)`` with every routing consumer replaced by a counter.

    Returns ``(rc, spies)`` where ``spies`` maps a consumer name to its counter:
    the five escalation dispatch targets, ``load_pcb_for_routing`` (every
    attempt loads through it), the optimizer's evaluation entry point
    ``RoutingOptimizer.optimize_net_order`` and ``_apply_order_method``.
    """
    from kicad_tools.cli import route_cmd
    from kicad_tools.optim import routing as optim_routing
    from kicad_tools.router import io as router_io

    spies: dict[str, _CallCounter] = {}
    for name in _DISPATCH_FUNCS:
        spies[name] = _CallCounter(name)
        monkeypatch.setattr(route_cmd, name, spies[name])

    class _LoadReached(Exception):
        pass

    def _load_spy(*args, **kwargs):
        spies["load_pcb_for_routing"].calls += 1
        raise _LoadReached("load reached")

    spies["load_pcb_for_routing"] = _CallCounter("load_pcb_for_routing")
    if real_load:
        _real_load = router_io.load_pcb_for_routing

        def _load_spy(*args, **kwargs):  # noqa: F811 - counting passthrough
            spies["load_pcb_for_routing"].calls += 1
            return _real_load(*args, **kwargs)

    monkeypatch.setattr(router_io, "load_pcb_for_routing", _load_spy)

    spies["optimize_net_order"] = _CallCounter("optimize_net_order")
    monkeypatch.setattr(
        optim_routing.RoutingOptimizer,
        "optimize_net_order",
        lambda self, *a, **k: (spies["optimize_net_order"](), ([], 0.0))[1],
    )
    real_apply = route_cmd._apply_order_method
    spies["_apply_order_method"] = _CallCounter("_apply_order_method")

    def _apply_spy(router, args, *a, **kwargs):
        spies["_apply_order_method"].calls += 1
        if stop_at_apply:
            raise _ApplyReached(args.order_method)
        return real_apply(router, args, *a, **kwargs)

    monkeypatch.setattr(route_cmd, "_apply_order_method", _apply_spy)

    try:
        rc = route_cmd.main(argv)
    except _LoadReached:
        rc = "load-reached"
    except _ApplyReached as exc:
        rc = f"apply-reached:{exc.args[0]}"
    return rc, spies


@pytest.mark.parametrize("quiet", [False, True], ids=["verbose", "quiet"])
@pytest.mark.parametrize("method", _SINGLE_ATTEMPT_ONLY)
@pytest.mark.parametrize(
    ("path_id", "extra", "dispatch"),
    _ESCALATION_PATHS,
    ids=[p[0] for p in _ESCALATION_PATHS],
)
def test_unsupported_order_method_rejected_before_routing_on_escalation_paths(
    tmp_path, monkeypatch, capsys, path_id, extra, dispatch, method, quiet
):
    """Every escalation path (and wrapper) rejects the four methods up front.

    Exit code 2, an actionable stderr message naming a supported alternative
    -- printed even under ``--quiet`` -- and zero calls into every routing
    consumer: no dispatch, no board load, no evaluation route.
    """
    pcb = _fixture_board(tmp_path)
    argv = [str(pcb), "-o", str(tmp_path / "out.kicad_pcb"), "--order-method", method, *extra]
    if quiet:
        argv.append("--quiet")

    rc, spies = _run_main_with_spies(argv, monkeypatch)

    assert rc == 2
    for name, spy in spies.items():
        assert spy.calls == 0, f"{name} was called {spy.calls}x for a rejected invocation"
    err = capsys.readouterr().err
    assert f"--order-method {method} is not supported" in err
    assert "Issue #5908" in err
    # Actionable: names both supported alternatives.
    assert "--no-auto-layers" in err
    assert "--order-method crossing" in err


@pytest.mark.parametrize("method", [None, "crossing"])
@pytest.mark.parametrize(
    ("path_id", "extra", "dispatch"),
    _ESCALATION_PATHS,
    ids=[p[0] for p in _ESCALATION_PATHS],
)
def test_absent_and_crossing_still_dispatch_on_escalation_paths(
    tmp_path, monkeypatch, capsys, path_id, extra, dispatch, method
):
    """Flag-absent and ``crossing`` are untouched by the #5908 gate.

    The invocation reaches exactly the expected dispatch target, with no
    evaluation route, no ``_apply_order_method`` at dispatch time and no new
    diagnostic.
    """
    pcb = _fixture_board(tmp_path)
    argv = [str(pcb), "-o", str(tmp_path / "out.kicad_pcb"), "--quiet", *extra]
    if method is not None:
        argv += ["--order-method", method]

    rc, spies = _run_main_with_spies(argv, monkeypatch)

    assert rc == 0
    assert spies[dispatch].calls == 1
    for name in _DISPATCH_FUNCS:
        if name != dispatch:
            assert spies[name].calls == 0, name
    assert spies["optimize_net_order"].calls == 0
    assert spies["_apply_order_method"].calls == 0
    captured = capsys.readouterr()
    assert "5908" not in captured.err
    assert "5908" not in captured.out


# Single-attempt invocations: ``--no-auto-layers`` and the documented
# fixed-layer path ``--layers N`` (which turns the default --auto-layers off,
# Issue #2388).
_SINGLE_ATTEMPT_PATHS = [
    ("no_auto_layers", ["--no-auto-layers"]),
    ("fixed_layers_2", ["--layers", "2"]),
    ("fixed_layers_4", ["--layers", "4"]),
]


@pytest.mark.parametrize("method", _SINGLE_ATTEMPT_ONLY)
@pytest.mark.parametrize(
    ("path_id", "extra"),
    _SINGLE_ATTEMPT_PATHS,
    ids=[p[0] for p in _SINGLE_ATTEMPT_PATHS],
)
def test_single_attempt_path_still_accepts_the_four_methods(
    tmp_path, monkeypatch, capsys, path_id, extra, method
):
    """``--no-auto-layers`` / ``--layers N`` keep the single-attempt path.

    The gate must not reject them: the invocation loads the board for real
    and reaches ``_apply_order_method`` with the requested method (our spy
    stops it there, before the evaluation/real route) without touching any
    escalation entry point.
    """
    pcb = _fixture_board(tmp_path)
    argv = [
        str(pcb),
        "-o",
        str(tmp_path / "out.kicad_pcb"),
        *extra,
        "--quiet",
        "--order-method",
        method,
    ]

    rc, spies = _run_main_with_spies(argv, monkeypatch, real_load=True, stop_at_apply=True)

    assert rc == f"apply-reached:{method}"
    assert spies["_apply_order_method"].calls == 1
    for name in _DISPATCH_FUNCS:
        assert spies[name].calls == 0, name
    assert "5908" not in capsys.readouterr().err


@pytest.mark.parametrize("method", [None, "crossing", *_SINGLE_ATTEMPT_ONLY])
def test_validate_order_method_single_attempt_namespace_is_allowed(method):
    from kicad_tools.cli.route_cmd import _validate_order_method_for_dispatch

    args = SimpleNamespace(
        order_method=method,
        auto_layers=False,
        adaptive_rules=False,
        auto_pcb_size=False,
        auto_mfr_tier=False,
    )
    assert _validate_order_method_for_dispatch(args) == 0


# ``--layers N --adaptive-rules`` is genuinely rule relaxation, not the
# fixed-layer single attempt, so the four methods must still be rejected.
_FIXED_LAYER_ESCALATION_PATHS = [
    ("fixed_layers_2_adaptive_rules", ["--layers", "2", "--adaptive-rules"], "--adaptive-rules"),
    ("fixed_layers_4_adaptive_rules", ["--layers", "4", "--adaptive-rules"], "--adaptive-rules"),
    ("fixed_layers_2_auto_mfr_tier", ["--layers", "2", "--auto-mfr-tier"], "--auto-mfr-tier"),
]


@pytest.mark.parametrize("method", _SINGLE_ATTEMPT_ONLY)
@pytest.mark.parametrize(
    ("path_id", "extra", "flag"),
    _FIXED_LAYER_ESCALATION_PATHS,
    ids=[p[0] for p in _FIXED_LAYER_ESCALATION_PATHS],
)
def test_fixed_layers_with_escalation_flag_still_rejected(
    tmp_path, monkeypatch, capsys, path_id, extra, flag, method
):
    pcb = _fixture_board(tmp_path)
    argv = [
        str(pcb),
        "-o",
        str(tmp_path / "out.kicad_pcb"),
        "--quiet",
        "--order-method",
        method,
        *extra,
    ]

    rc, spies = _run_main_with_spies(argv, monkeypatch)

    assert rc == 2
    for name, spy in spies.items():
        assert spy.calls == 0, f"{name} was called {spy.calls}x for a rejected invocation"
    err = capsys.readouterr().err
    assert f"--order-method {method} is not supported" in err
    assert flag in err
    # The user never selected --auto-layers: --layers N turned it off.
    assert "--auto-layers (the default)" not in err
    assert "--layers N" in err
    assert "--order-method crossing" in err


@pytest.mark.parametrize("method", _SINGLE_ATTEMPT_ONLY)
def test_explicit_auto_layers_with_layers_conflict_keeps_priority(
    tmp_path, monkeypatch, capsys, method
):
    """``--auto-layers --layers N`` still fails with the #2388 exit-1 error."""
    pcb = _fixture_board(tmp_path)
    argv = [
        str(pcb),
        "-o",
        str(tmp_path / "out.kicad_pcb"),
        "--quiet",
        "--auto-layers",
        "--layers",
        "2",
        "--order-method",
        method,
    ]

    rc, spies = _run_main_with_spies(argv, monkeypatch)

    assert rc == 1
    for name in _DISPATCH_FUNCS:
        assert spies[name].calls == 0, name
    assert spies["optimize_net_order"].calls == 0
    err = capsys.readouterr().err
    assert "--auto-layers cannot be used with --layers 2" in err
    assert "5908" not in err


@pytest.mark.parametrize("method", _SINGLE_ATTEMPT_ONLY)
@pytest.mark.parametrize("layers", ["2", "4"])
def test_validate_order_method_fixed_layers_namespace_is_allowed(method, layers):
    """Default --auto-layers plus an explicit --layers N is a single attempt."""
    from kicad_tools.cli.route_cmd import _validate_order_method_for_dispatch

    args = SimpleNamespace(
        order_method=method,
        auto_layers=True,
        layers=layers,
        adaptive_rules=False,
        auto_pcb_size=False,
        auto_mfr_tier=False,
    )
    argv = ["b.kicad_pcb", "--layers", layers, "--order-method", method]
    assert _validate_order_method_for_dispatch(args, argv) == 0
